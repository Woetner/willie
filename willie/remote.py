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
       willie/tool/result  {"id", "result"}
       willie/hub/call     {"id", "name", "args"}  a tool that runs on the server (reminders)
       willie/improve/request  a code change waiting for approval in the app (tools.verbeter_jezelf)
  in   willie/cmd/say      {"text"}             speak it aloud in the room
       willie/cmd/photo    {}                   one still -> willie/photo
       willie/cmd/video    {"on": true|false}   live view; must be repeated every few seconds
       willie/cmd/mode     {"mode": "idle"|"sleep"}  sleep = mic off + sleep face (privacy.mute)
                           {"mode": "garage"|"garage_off"}  garage mode (K1)
       willie/cmd/power    {"action": "off"|"restart"}  the app asked "are you sure?" already
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

log = logging.getLogger("willie.remote")

STATE_EVERY_S = 2.0
HEALTH_EVERY_S = 10.0
ACTIVITY_STALE_S = 180.0    # no fresh activity list from the hub: the icons go (hub down)
VIDEO_LEASE_S = 10.0        # live view stops by itself when the hub stops asking (S8)
VIDEO_SIZE, VIDEO_FPS, VIDEO_QUALITY = (640, 480), 10, 60
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

    def close(self) -> None:
        self._stop.set()
        self._video_until = 0
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
                          ("willie/activity", 1), ("willie/improve/decision", 1)])
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
        handler = {
            "willie/cmd/say": self._say,
            "willie/cmd/photo": self._photo,
            "willie/cmd/video": self._video,
            "willie/cmd/mode": self._mode,
            "willie/cmd/power": self._power,
            "willie/tool/call": self._tool,
        }.get(message.topic)
        if handler is None:
            return
        if message.topic == "willie/cmd/video":
            handler(data)                            # cheap, keeps the lease exact
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

    def _mode(self, data: dict) -> None:
        """Idle (awake, listening for "Hey Willie") or sleep (mic off, sleep face). Stored as
        privacy.mute, so it survives a restart and the dashboard shows the same switch."""
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
        if not self.session_active:
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
                ["rpicam-vid", "--nopreview", "-t", "0", "--codec", "mjpeg", "-q", str(VIDEO_QUALITY),
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
                        self._latest_frame = frame
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
                    self.face.indicators(watched=False, camera=False)
                self._publish("willie/event/video", {"on": False})
                log.info("live view off")

    def latest_frame(self) -> bytes | None:
        """For kijk(): while live view holds the camera, look at the stream instead."""
        return self._latest_frame if self._video_running() else None

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
