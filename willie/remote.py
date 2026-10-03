"""The robot's side of the phone app (S6, S8, S10, S14): an MQTT bridge to the home server.

Runs inside the voice process (`tools/willie_voice.py`), because that process owns what the
phone drives: the face, the speaker and the camera. The core's MCU state comes in through
the `state.json` it already writes.

It never blocks the voice loop: paho runs its own network thread, commands run in short
worker threads, and with the broker gone everything is simply dropped (D22 - the robot
works the same without the server).

Topics (WILL-E.md Phase S):
  out  willie/online       "1" / "0" (retained, "0" is the last will)
       willie/state        JSON every 2 s: face, mic/camera, Pi health, core + MCU link
       willie/tools        the voice tool list (retained), so the hub's chat and phone
       willie/persona      call use exactly the robot's tools and character
       willie/photo        one JPEG, answer to cmd/photo
       willie/video        MJPEG frames while live view is on
       willie/audio        raw 16 kHz mono S16 chunks while listening is on
       willie/event/drive|listen|ptt  {"ok", ...}  what the last command did
       willie/tool/result  {"id", "result"}
       willie/hub/call     {"id", "name", "args"}  a tool that runs on the server (reminders)
       willie/improve/request  a code change waiting for approval in the app (tools.verbeter_jezelf)
  in   willie/cmd/say      {"text"}             speak it aloud in the room
       willie/cmd/photo    {}                   one still -> willie/photo
       willie/cmd/video    {"on": true|false}   live view; must be repeated every few seconds
       willie/cmd/mode     {"mode": "idle"|"sleep"}  sleep = mic off + sleep face (privacy.mute)
                           {"mode": "garage"|"garage_off"}  garage mode (K1)
       willie/cmd/power    {"action": "off"|"restart"}  the app asked "are you sure?" already
       willie/cmd/look     {"pan", "tilt"}      move the camera head (degrees, limited by the firmware)
       willie/cmd/drive_arm {"on": true|false}  arm WASD / joystick driving for 10 min (control.py manual_*)
       willie/cmd/drive    {"v", "w", "seq"}    fractions -1..1 of the speed limits, 10 Hz; silence = stop
       willie/cmd/listen   {"on": true|false}   hear the room: willie/audio frames; repeat every few seconds
       willie/ptt/audio    raw PCM            push-to-talk: the speaker plays it; the face shows "watched"
       willie/tool/call    {"id", "name", "args"}
       willie/hub/result   {"id", "result"}     answer to willie/hub/call
       willie/improve/decision {"id", "approved", "hash"}  Wouter's tap in the app
       willie/activity     {"time", "now", "background"} (retained, hub every <= 60 s):
                           background jobs -> small icons on his face
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from willie import camera

log = logging.getLogger("willie.remote")

STATE_EVERY_S = 2.0
HEALTH_EVERY_S = 10.0
ACTIVITY_STALE_S = 180.0    # no fresh activity list from the hub: the icons go (hub down)
VIDEO_LEASE_S = 10.0        # live view stops by itself when the hub stops asking (S8)
VIDEO_SIZE, VIDEO_FPS, VIDEO_QUALITY = (640, 360), 10, 50      # 16:9 = the whole sensor width, about 14 KB a frame
LISTEN_LEASE_S = 10.0       # listening through his mics stops by itself when the app stops asking
PTT_IDLE_S = 1.5            # push-to-talk ends when no audio has come for this long
AUDIO_RATE = 16_000         # willie/audio (out) and willie/ptt/audio (in): raw mono S16 little-endian
# Tools the phone's chat and call may not run on the robot (security audit, 24 Sep): power
# has its own button with "are you sure?" in the app, and on the robot it needs a finger on
# the screen, which nobody on the phone can give.
REMOTE_REFUSED = {"zet_uit"}


def _core_state_file() -> Path:
    from willie import log as wlog
    return wlog.DATA_DIR / "state.json"


class Remote:
    def __init__(self, face=None, host: str = "", user: str = "", password: str = "", port: int = 1883,
                 wake_stop=None, sleep_now=None):
        self.face = face
        self.wake_stop = wake_stop           # threading.Event: stop the wake-word wait now
        self.sleep_now = sleep_now           # threading.Event: end a running conversation now
        self.host, self.port, self.user, self.password = host, port, user, password
        self.session_active = False          # set by the voice loop: the speaker is taken
        self._activity_at = 0.0              # monotonic time of the last fresh willie/activity
        self._activity_sent = 0.0
        self.mode_shown = None               # "idle"/"sleep" as last applied by _mode (Phase P watcher)
        self.client = None
        self._stop = threading.Event()
        self._camera = threading.Lock()      # one owner of the camera at a time (S8 design point)
        self._video_until = 0.0
        self._video_thread: threading.Thread | None = None
        self._latest_frame: bytes | None = None
        self._health: dict = {}
        self._health_at = 0.0
        self._hub_calls: dict[str, dict] = {}     # id -> {"done": Event, "result": ...}
        self.garage_control: dict | None = None  # garage mode (K1): {"say": fn(text)} while connected
        self._listen_until = 0.0              # monotonic: the app hears the room until then
        self._ptt_proc = None                # aplay for push-to-talk, open while audio keeps coming
        self._ptt_last = 0.0
        self._ptt_lock = threading.Lock()
        self._drive_seq = -1
        self._local_listeners: list = []     # sockets of the Pi's own dashboard listening (listen.sock)
        self._local_servers: list = []

    # ---------------------------------------------------------------- lifecycle
    @classmethod
    def from_env(cls, face=None, wake_stop=None, sleep_now=None) -> "Remote | None":
        host = os.environ.get("MQTT_HOST", "")
        if not host:
            log.info("remote off: no MQTT_HOST in .env")
            return None
        try:
            import paho.mqtt.client  # noqa: F401
        except ImportError:
            log.warning("remote off: paho-mqtt missing - run `make deps`")
            return None
        remote = cls(face, host, os.environ.get("MQTT_USER", ""), os.environ.get("MQTT_PASS", ""),
                     int(os.environ.get("MQTT_PORT", "1883")), wake_stop, sleep_now)
        remote.start()
        return remote

    def start(self) -> None:
        import paho.mqtt.client as mqtt

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="willie-robot")
        if self.user:
            client.username_pw_set(self.user, self.password)
        client.will_set("willie/online", "0", qos=1, retain=True)
        client.reconnect_delay_set(1, 10)          # back within 10 s of the server (S6)
        client.max_queued_messages_set(20)         # broker gone: drop, never pile up
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = lambda *a, **k: self._activity(None)
        self.client = client
        client.connect_async(self.host, self.port, keepalive=15)
        client.loop_start()
        threading.Thread(target=self._state_loop, name="willie-remote-state", daemon=True).start()
        log.info("remote bridge -> %s:%d", self.host, self.port)
        self._serve_local()

    def close(self) -> None:
        self._stop.set()
        self._video_until = 0
        for server, path in self._local_servers:
            try:
                server.close()
                path.unlink()
            except OSError:
                pass
        if self.client:
            try:
                self.client.publish("willie/online", "0", qos=1, retain=True).wait_for_publish(1)
            except Exception:
                pass
            self.client.loop_stop()
            self.client.disconnect()

    # ---------------------------------------------------------------- MQTT
    def _publish(self, topic: str, payload, retain: bool = False, qos: int = 0) -> None:
        if self.client is None or not self.client.is_connected():
            return
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=False)
        self.client.publish(topic, payload, qos=qos, retain=retain)

    def _on_connect(self, client, userdata, flags, reason, properties=None):
        if getattr(reason, "is_failure", False):
            log.warning("MQTT refused: %s", reason)
            return
        log.info("MQTT connected")
        client.subscribe([("willie/cmd/#", 1), ("willie/tool/call", 1), ("willie/hub/result", 1),
                          ("willie/activity", 1), ("willie/improve/decision", 1), ("willie/ptt/audio", 0)])
        self._publish("willie/online", "1", retain=True, qos=1)
        try:
            from willie.voice import tools
            from willie.voice.persona import system_prompt

            # The robot's own tool list (voice/tools.py); face-only tools stay on the robot.
            self._publish("willie/tools", tools.declarations(), retain=True, qos=1)
            # Without his memory: that lives on the server now, and the hub adds it itself.
            self._publish("willie/persona", system_prompt(), retain=True, qos=1)
            # Improvement requests still waiting: show them in the app again (the hub
            # answers a request it already decided with its decision).
            for entry in tools.pending_requests():
                self._publish("willie/improve/request", entry, qos=1)
        except Exception:
            log.exception("could not publish tools/persona")

    def _on_message(self, client, userdata, message):
        if message.topic == "willie/ptt/audio":      # raw PCM, not JSON
            self._ptt_audio(message.payload)
            return
        try:
            data = json.loads(message.payload or b"{}")
        except ValueError:
            data = {}
        if message.topic == "willie/hub/result":
            waiter = self._hub_calls.get(str(data.get("id")))
            if waiter:
                waiter["result"] = data.get("result")
                waiter["done"].set()
            return
        if message.topic == "willie/activity":
            self._activity(data)                     # cheap: sets a tuple on the face
            return
        if message.topic == "willie/improve/decision":
            threading.Thread(target=self._guard, args=(self._decision, data), daemon=True).start()
            return
        if message.topic in ("willie/cmd/look", "willie/cmd/drive_arm", "willie/cmd/drive") and "t" in data:
            data["_rx"] = time.time()
            age = (data["_rx"] - float(data["t"])) * 1000      # hub clock to this clock (both NTP): delivery + queueing
            if age > 150 or message.topic == "willie/cmd/drive_arm":
                log.info("%s arrived %.0f ms after the hub sent it", message.topic.rsplit("/", 1)[1], age)
        handler = {
            "willie/cmd/say": self._say,
            "willie/cmd/photo": self._photo,
            "willie/cmd/video": self._video,
            "willie/cmd/mode": self._mode,
            "willie/cmd/power": self._power,
            "willie/cmd/look": self._look,
            "willie/cmd/drive_arm": self._drive_arm,
            "willie/cmd/drive": self._drive,
            "willie/cmd/listen": self._listen,
            "willie/tool/call": self._tool,
        }.get(message.topic)
        if handler is None:
            return
        if message.topic != "willie/cmd/mode" and time.monotonic() - self._activity_sent > 30:
            self._activity_sent = time.monotonic()   # the app is in use: no auto-sleep (Phase P)
            from willie import control
            control.event("activity")
        if message.topic in ("willie/cmd/video", "willie/cmd/listen", "willie/cmd/drive"):
            handler(data)                            # cheap, keeps the lease / the 10 Hz rhythm exact
        else:
            threading.Thread(target=self._guard, args=(handler, data), daemon=True).start()

    @staticmethod
    def _guard(handler, data):
        try:
            handler(data)
        except Exception:
            log.exception("remote command failed")

    # ---------------------------------------------------------------- commands
    def _say(self, data: dict) -> None:
        text = str(data.get("text", "")).strip()[:500]
        if not text:
            return
        say_in_session = (self.garage_control or {}).get("say")
        if say_in_session:
            # Garage mode is one long conversation: say it inside it, in his own voice.
            say_in_session(f"[ROBOT] Zeg dit nu hardop tegen Wouter: {text}")
            self._publish("willie/event/say", {"ok": True, "text": text, "how": "in the garage conversation"})
            return
        if self.session_active:
            self._publish("willie/event/say", {"ok": False, "text": text, "why": "in a conversation"})
            return
        if _asleep():
            self._publish("willie/event/say", {"ok": False, "text": text, "why": "he is asleep"})
            return
        from willie.audio import speech

        log.info("say (phone): %s", text)
        if self.face:
            self.face.set_state("talking")
        try:
            backend = speech.speak(text)
        finally:
            if self.face:
                self.face.set_state("idle", "SAY HEY WILLIE")
        self._publish("willie/event/say", {"ok": bool(backend), "text": text})

    def _mode(self, data: dict, chime_on: bool = True) -> None:
        """Idle (awake, listening for "Hey Willie") or sleep (mic off, sleep face). Stored as
        privacy.mute, so it survives a restart and the dashboard shows the same switch.
        Also called for a tap on the sleeping screen and, without the chime, when the core
        put him to sleep or woke him by itself (Phase P, willie/sleep.py)."""
        mode = str(data.get("mode", ""))
        if mode in ("garage", "garage_off"):
            # Garage mode (K1) from the app: the voice loop picks it up within 2 s.
            from willie.voice import garage
            garage.set_enabled(mode == "garage")
            if mode == "garage" and self.wake_stop:
                self.wake_stop.set()
            self._publish("willie/event/mode", {"mode": mode})
            return
        if mode not in ("idle", "sleep"):
            return
        self.mode_shown = mode               # first, so the sleep-switch watcher does not repeat it
        from willie.config import Config

        cfg = Config()
        if bool(cfg.get("privacy.mute")) != (mode == "sleep"):
            cfg.update({"privacy": {"mute": mode == "sleep"}})
            log.info("mode (phone): %s", mode)
        if mode == "sleep":
            # Sleep always wins: stop talking mid-word and end the conversation (Wouter, 22 Sep).
            from willie.audio import speech
            if self.sleep_now:
                self.sleep_now.set()
            speech.silence(True)
            for _ in range(30):              # the session closes within ~0.5 s; wait max 3 s
                if not self.session_active:
                    break
                time.sleep(0.1)
        else:
            from willie.audio import speech
            speech.silence(False)
        if self.face:
            self.face.indicators(muted=mode == "sleep", mic=False)
            self.face.set_state("sleep" if mode == "sleep" else "idle", "" if mode == "sleep" else "SAY HEY WILLIE")
        if self.wake_stop:
            self.wake_stop.set()             # the voice loop re-reads the mode right away
        if chime_on and not self.session_active:
            from willie.audio import chime
            chime.play(mode)
        self._publish("willie/event/mode", {"mode": mode})

    def _power(self, data: dict) -> None:
        from willie.voice import tools

        action = {"off": "uit", "restart": "herstart"}.get(str(data.get("action", "")))
        if action:
            log.info("power (phone): %s", action)
            tools.power(action)                  # the app asked "are you sure?" already

    def request_approval(self, entry: dict) -> bool:
        """tools.APPROVAL_HOOK: an improvement request goes to the app for Wouter's tap."""
        if self.client is None or not self.client.is_connected():
            return False
        self._publish("willie/improve/request", entry, qos=1)
        return True

    def _decision(self, data: dict) -> None:
        from willie.voice import tools

        status = tools.decide(str(data.get("id", "")), data.get("approved") is True, str(data.get("hash", "")))
        log.info("improvement %s: %s", data.get("id"), status)
        self._publish("willie/event/improve", {"id": data.get("id"), "status": status})

    def powering(self, actie: str) -> None:
        """tools.POWER_HOOK: last things before poweroff/reboot."""
        self._publish("willie/event/power", {"action": "off" if actie == "uit" else "restart"}, qos=1)
        self._publish("willie/online", "0", retain=True, qos=1)
        if self.face:
            self.face.indicators(mic=False, camera=False, watched=False)
            self.face.set_state("sleep")
        if not self.session_active:
            from willie.audio import chime
            chime.play("sleep")

    def _photo(self, data: dict) -> None:
        jpeg = self.photo()
        if jpeg:
            self._publish("willie/photo", jpeg, qos=1)
        else:
            self._publish("willie/event/photo", {"ok": False})

    def photo(self) -> bytes | None:
        """One JPEG: the newest live-view frame while streaming, otherwise a real still."""
        if self._video_running() and self._latest_frame:
            return self._latest_frame
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        from ask_camera import capture

        with self._camera, tempfile.TemporaryDirectory(prefix="willie-photo-") as tmp:
            path = Path(tmp) / "photo.jpg"
            if self.face:
                self.face.indicators(camera=True)
            try:
                capture(path, quiet=True)
            except RuntimeError as exc:
                log.warning("photo failed: %s", exc)
                return None
            finally:
                if self.face:
                    self.face.indicators(camera=self._video_running())
            return path.read_bytes() if path.exists() else None

    def _video(self, data: dict) -> None:
        if not data.get("on"):
            self._video_until = 0
            return
        self._video_until = time.monotonic() + VIDEO_LEASE_S
        if not self._video_running():
            self._video_thread = threading.Thread(target=self._stream, name="willie-video", daemon=True)
            self._video_thread.start()

    def _video_running(self) -> bool:
        return self._video_thread is not None and self._video_thread.is_alive()

    def _stream(self) -> None:
        """rpicam-vid MJPEG (hardware encoder) -> one MQTT message per frame (S8 v1)."""
        if not shutil.which("rpicam-vid"):
            log.warning("live view: rpicam-vid missing")
            return
        w, h = VIDEO_SIZE
        with self._camera:
            log.info("live view on")
            if self.face:
                self.face.indicators(watched=True, camera=True)
            self._publish("willie/event/video", {"on": True})
            proc = subprocess.Popen(
                ["rpicam-vid", "--nopreview", "-t", "0", *camera.mode_args(), "--codec", "mjpeg", "-q", str(VIDEO_QUALITY),
                 "--width", str(w), "--height", str(h), "--framerate", str(VIDEO_FPS),
                 "--autofocus-mode", "continuous", "--flush", "-o", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            buffer = b""
            try:
                while time.monotonic() < self._video_until and not self._stop.is_set():
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    buffer += chunk
                    while True:           # frames are SOI (FFD8) ... EOI (FFD9)
                        start = buffer.find(b"\xff\xd8")
                        end = buffer.find(b"\xff\xd9", start + 2)
                        if start < 0 or end < 0:
                            break
                        frame, buffer = buffer[start:end + 2], buffer[end + 2:]
                        self._latest_frame = frame             # raw: the app turns it (camera_rotation in the state)
                        self._publish("willie/video", frame)
                    if len(buffer) > 2_000_000:
                        buffer = b""
            finally:
                proc.terminate()
                try:
                    proc.wait(3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                self._latest_frame = None
                if self.face:
                    self.face.indicators(watched=self._listening() or self._ptt_open(), camera=False)
                self._publish("willie/event/video", {"on": False})
                log.info("live view off")

    # ---------------------------------------------------------------- camera head, driving
    def _look(self, data: dict) -> None:
        from willie import control
        pan = max(-95.0, min(95.0, float(data.get("pan", 0))))
        tilt = max(-90.0, min(90.0, float(data.get("tilt", 0))))
        control.request("look", timeout=1.0, pan=pan, tilt=tilt)

    def _drive_arm(self, data: dict) -> None:
        from willie import control
        on = bool(data.get("on"))
        r = control.request("manual_arm", timeout=1.0, on=on)
        if "_rx" in data:
            log.info("drive_arm handled in %.0f ms (waited for a thread + the core)", (time.time() - data["_rx"]) * 1000)
        self._publish("willie/event/drive", {"ok": True, "armed": bool(r.get("armed")), "seconds_left": r.get("seconds_left", 0)})

    def _drive(self, data: dict) -> None:
        """10 Hz from the app. Goes through control.py, so Safety.gate() (speed limits, obstacle slow-down, cliff
        and tilt stops) and the 200 ms firmware watchdog apply: when the app stops sending, the wheels stop."""
        from willie import control
        seq = int(data.get("seq", 0))
        if 0 <= seq < self._drive_seq and self._drive_seq - seq < 1000:
            return                                   # an old command that arrived late
        self._drive_seq = seq
        r = control.request("manual_drive", timeout=0.5, v=float(data.get("v", 0)), w=float(data.get("w", 0)))
        if r.get("ok") is False or r.get("fout"):
            self._publish("willie/event/drive", {"ok": False, "why": r.get("reden") or r.get("fout")})

    # ---------------------------------------------------------------- the Pi's own dashboard (same machine)
    # The dashboard is its own process, so it cannot reach the mic loop or the speaker. Two unix sockets next to the
    # control socket: listen.sock streams the room as raw 16 kHz mono S16 to whoever connects, talk.sock takes raw
    # 16 kHz mono S16 and plays it like willie/ptt/audio. Same rules: the face shows "watched", talking is refused in a
    # conversation, the wake word is held off while it plays.
    def _serve_local(self) -> None:
        import socket as _socket
        from willie import log as wlog
        for name, loop in (("listen.sock", self._local_listen_loop), ("talk.sock", self._local_talk_loop)):
            path = wlog.DATA_DIR / name
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            server = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            server.bind(str(path))
            os.chmod(path, 0o660)
            server.listen(2)
            self._local_servers.append((server, path))
            threading.Thread(target=loop, args=(server,), name=f"willie-{name}", daemon=True).start()

    def _local_listen_loop(self, server) -> None:
        from willie.audio import mic
        while not self._stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                return
            conn.settimeout(0.05)
            self._local_listeners.append(conn)
            mic.TAP = self._mic_tap
            log.info("listening (dashboard) on")
            self._refresh_watched()

    def _local_drop(self, conn) -> None:
        try:
            self._local_listeners.remove(conn)
            conn.close()
        except (ValueError, OSError):
            pass
        log.info("listening (dashboard) off")
        self._refresh_watched()

    def _local_talk_loop(self, server) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=self._local_talk_client, args=(conn,), name="willie-talk", daemon=True).start()

    def _local_talk_client(self, conn) -> None:
        rest = b""
        with conn:
            while not self._stop.is_set():
                try:
                    data = conn.recv(4096)
                except OSError:
                    break
                if not data:
                    break
                data = rest + data
                cut = len(data) // 2 * 2
                data, rest = data[:cut], data[cut:]
                if data:
                    self._ptt_audio(data)

    # ---------------------------------------------------------------- listening, push-to-talk
    def _listening(self) -> bool:
        return time.monotonic() < self._listen_until or bool(self._local_listeners)

    def _ptt_open(self) -> bool:
        return self._ptt_proc is not None

    def _refresh_watched(self) -> None:
        if self.face:
            self.face.indicators(watched=self._video_running() or self._listening() or self._ptt_open())

    def _listen(self, data: dict) -> None:
        """Hear the room through his mics (the wake word / conversation loop tees its chunks into _mic_tap). He
        shows the 'watched' face the whole time, like the live view (D17)."""
        from willie.audio import mic
        if not data.get("on"):
            self._listen_until = 0.0
            if mic.TAP == self._mic_tap:
                mic.TAP = None
            self._refresh_watched()
            return
        if _asleep():
            self._publish("willie/event/listen", {"ok": False, "why": "he is asleep (mic off)"})
            return
        was = self._listening()
        self._listen_until = time.monotonic() + LISTEN_LEASE_S
        mic.TAP = self._mic_tap
        if not was:
            log.info("listening (phone) on")
            self._refresh_watched()
            self._publish("willie/event/listen", {"ok": True, "on": True})

    def _mic_tap(self, chunk: bytes) -> None:
        if not self._listening():
            from willie.audio import mic
            if mic.TAP == self._mic_tap:
                mic.TAP = None
            self._refresh_watched()
            log.info("listening (phone) off")
            self._publish("willie/event/listen", {"ok": True, "on": False})
            return
        if self._ptt_open():
            return                                   # the one who talks does not hear himself
        if time.monotonic() < self._listen_until:
            self._publish("willie/audio", chunk)
        for conn in list(self._local_listeners):
            try:
                conn.send(chunk)
            except TimeoutError:
                pass                                 # a slow page: this chunk is lost, the next one is newer
            except OSError:
                self._local_drop(conn)

    def _ptt_audio(self, pcm: bytes) -> None:
        """Push-to-talk: raw 16 kHz mono S16 from the app, played on his speaker as it comes. Refused while he
        is in a conversation (the speaker is his) or asleep. The card is mute at 16 kHz, so it is repeated up to
        48 kHz."""
        if self.session_active or _asleep() or not pcm:
            return
        import numpy as np
        from willie.audio import speech
        samples = np.frombuffer(pcm[: len(pcm) // 2 * 2], "<i2")
        loud = speech.scale(samples.repeat(speech.CARD_RATE // AUDIO_RATE).astype("<i2").tobytes())
        from willie.voice import wake
        wake.HOLD_UNTIL = time.monotonic() + PTT_IDLE_S + 1.0     # his own speaker must not wake him
        with self._ptt_lock:
            self._ptt_last = time.monotonic()                # before the watcher exists, or it sees an old time and closes at once
            if self._ptt_proc is None:
                self._ptt_proc = subprocess.Popen(
                    ["aplay", "-q", "-D", speech.DEVICE, "-f", "S16_LE", "-r", str(speech.CARD_RATE), "-c", "1",
                     "-t", "raw"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                log.info("push-to-talk on")
                self._refresh_watched()
                self._publish("willie/event/ptt", {"ok": True, "on": True})
                threading.Thread(target=self._ptt_watch, name="willie-ptt", daemon=True).start()
            try:
                self._ptt_proc.stdin.write(loud)
                self._ptt_proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._ptt_close()

    def _ptt_watch(self) -> None:
        while self._ptt_open() and not self._stop.is_set():
            if time.monotonic() - self._ptt_last > PTT_IDLE_S:
                with self._ptt_lock:
                    self._ptt_close()
                return
            time.sleep(0.2)

    def _ptt_close(self) -> None:
        proc, self._ptt_proc = self._ptt_proc, None
        if proc is not None:
            try:
                proc.stdin.close()
                proc.wait(2)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
            log.info("push-to-talk off")
            self._refresh_watched()
            self._publish("willie/event/ptt", {"ok": True, "on": False})

    def latest_frame(self) -> bytes | None:
        """For kijk(): while live view holds the camera, look at the stream instead."""
        frame = self._latest_frame if self._video_running() else None
        return camera.rotate_jpeg(frame) if frame else None     # one frame, so the Pi turns it for the model

    def hub_call(self, name: str, args: dict, timeout: float = 20.0) -> dict:
        """Run a server-side tool (reminders, H4) from the voice session; blocks up to `timeout`."""
        if self.client is None or not self.client.is_connected():
            return {"fout": "De thuisserver is niet bereikbaar, dus dit kan nu niet."}
        call_id = os.urandom(6).hex()
        waiter = {"done": threading.Event(), "result": None}
        self._hub_calls[call_id] = waiter
        try:
            self._publish("willie/hub/call", {"id": call_id, "name": name, "args": args}, qos=1)
            if not waiter["done"].wait(timeout):
                return {"fout": f"De thuisserver gaf binnen {timeout:.0f} s geen antwoord."}
        finally:
            self._hub_calls.pop(call_id, None)
        result = waiter["result"]
        return result if isinstance(result, dict) else {"resultaat": result}

    def _tool(self, data: dict) -> None:
        from willie.voice import tools

        name, call_id = str(data.get("name", "")), data.get("id")
        log.info("tool (phone): %s", name)
        if name in REMOTE_REFUSED:
            result = {"fout": f"{name} kan niet vanaf de telefoon; gebruik de knop in de app."}
        else:
            result = tools.call(name, data.get("args") or {})
        self._publish("willie/tool/result", {"id": call_id, "name": name, "result": result}, qos=1)

    # ---------------------------------------------------------------- state
    def _activity(self, data: dict | None) -> None:
        if not self.face:
            return
        from willie.face.runtime import activity_badges
        fresh = isinstance(data, dict) and time.time() - float(data.get("time") or 0) < ACTIVITY_STALE_S
        self._activity_at = time.monotonic() if fresh else 0.0
        self.face.activity(activity_badges(data) if fresh else [])

    def _state_loop(self) -> None:
        while not self._stop.wait(STATE_EVERY_S):
            if self._activity_at and time.monotonic() - self._activity_at > ACTIVITY_STALE_S:
                self._activity(None)
            try:
                self._publish("willie/state", self.state())
            except Exception:
                log.exception("state failed")

    def state(self) -> dict:
        now = time.monotonic()
        if now - self._health_at > HEALTH_EVERY_S:
            self._health, self._health_at = _pi_health(), now
        core = None
        try:
            core = json.loads(_core_state_file().read_text())
            core["age_s"] = round(time.time() - core.get("time", 0), 1)
        except (OSError, ValueError):
            pass
        face = None
        if self.face:
            v = self.face.snapshot()
            face = {"state": v.state, "mic": v.mic, "camera": v.camera, "muted": v.muted,
                    "watched": v.watched, "battery": v.battery}
        garage = False
        try:
            from willie.config import Config
            cfg = Config()
            mode = "sleep" if cfg.get("privacy.mute") else "idle"
            garage = bool(cfg.get("modes.garage"))
        except Exception:
            mode = None
        try:
            from willie import missions
            mission = missions.status()
        except Exception:
            mission = None
        return {"time": time.time(), "face": face, "mode": mode, "garage": garage, "mission": mission,
                "pi": self._health, "core": core,
                "talking": self.session_active, "video": self._video_running(),
                "camera_rotation": camera.rotation(),
                "voice_rss_mb": _rss_mb()}


def _asleep() -> bool:
    try:
        from willie.config import Config
        return bool(Config().get("privacy.mute"))
    except Exception:
        return False


def _rss_mb() -> float | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return None


def _pi_health() -> dict:
    def run(*cmd) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    health: dict = {}
    temp = run("vcgencmd", "measure_temp").replace("temp=", "").replace("'C", "")
    try:
        health["temp_c"] = float(temp)
    except ValueError:
        pass
    throttled = run("vcgencmd", "get_throttled").replace("throttled=", "")
    if throttled:
        health["throttled"] = throttled
    try:
        mem = {l.split(":")[0]: int(l.split()[1]) for l in Path("/proc/meminfo").read_text().splitlines()}
        health["mem_free_mb"] = mem.get("MemAvailable", 0) // 1024
        health["load"] = float(Path("/proc/loadavg").read_text().split()[0])
        health["uptime_s"] = int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError):
        pass
    services = run("systemctl", "is-active", "willie", "willie-voice").split()
    if len(services) == 2:
        health["services"] = {"willie": services[0], "willie-voice": services[1]}
    wifi = run("iwgetid", "-r")
    if wifi:
        health["wifi"] = wifi
    signal = run("sh", "-c", "grep wlan0 /proc/net/wireless | awk '{print $4}'").rstrip(".")
    if signal:
        health["wifi_dbm"] = signal
    return health
