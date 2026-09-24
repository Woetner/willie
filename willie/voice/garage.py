"""Garage mode (K1, D25): a workshop buddy that is always listening, but only to Wouter.

`run()` replaces the wake-word loop while `modes.garage` is on:

- one Live conversation after another, each continuing the last (session resumption),
  so an afternoon in the garage is one conversation with one context
- the voice gate (willie/voice/gate.py): each utterance is checked on the home server
  (S15, `stem_check`) before it reaches Gemini; without the server, "Hey Willie" again
- the workshop instructions in the prompt (GARAGE_PROMPT)
- a danger watch: every `garage.watch_s` one photo + the air values go to the server
  (`garage_gevaar`); a clear danger is said once, in the conversation
- no driving: the safety gate refuses motion while garage mode is on (he stands on the
  workbench; F1's cliff rule has not been proven yet)

Everything that is not on the robot degrades as D22 asks: the gate falls back to the
wake word, the danger watch just stops.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("willie.garage")

PROBE_EVERY_S = 60.0          # while the voice check is down: ask the server again this often
CHECK_TIMEOUT_S = 3.0
RETRY_S = (2, 5, 10, 30)      # reconnect back-off after a failed session start

GARAGE_PROMPT = """GARAGEMODUS staat aan. Je staat op Wouters werkbank in de garage en luistert zonder 'Hey Willie'; \
alleen zijn stem komt bij je binnen. Je bent zijn werkplaatsmaatje:
- Onderdelen en elektronica: gebruik pinout voor elke vraag over pinnen, aansluitingen of een pinout; die tool \
zet de pinout op je gezicht en controleert hem tegen de datasheet. Zeg het als hij NIET GECONTROLEERD is.
- Voor 'wat is dit?' en 'klopt mijn bedrading?': gebruik kijk met een precieze vraag (naam, waarde, kleurcode, \
functie, gevaren; of: vergelijk met het bedoelde schema).
- Kever en Tomos: gebruik voertuig voor wat Wouter zelf over zijn voertuig heeft vastgelegd, en \
voertuig_log om een klus of meting te bewaren. Specificaties uit je hoofd: noem ze met 'controleer in het \
werkplaatshandboek' als je niet zeker bent. Nooit een aanhaalmoment gokken.
- Stappenplannen (volgorde, aanhaalmomenten): gebruik stappenplan; lees daarna één stap per keer voor en wacht \
tot hij 'volgende' zegt. Voor wachttijden (lijm, epoxy, afkoelen) zet je een timer met bewaak soort timer.
- 'Ik heb X nodig' of 'zet op de lijst': boodschappen.
- Veiligheid: waarschuw één keer, kort en duidelijk, als iets gevaarlijk is (auto op de krik zonder bok, \
LiPo, netspanning, dampen, hete bout), daarna niet blijven zeuren.
- Berichten die beginnen met [ROBOT] komen van jezelf (een timer, iets dat je zag): zeg ze meteen kort hardop.
- Garagemodus uit: alleen als Wouter dat vraagt (garagemodus aan=false)."""


def _config():
    from willie.config import Config
    return Config()


def enabled() -> bool:
    try:
        return bool(_config().get("modes.garage"))
    except Exception:
        return False


def set_enabled(on: bool) -> None:
    _config().update({"modes": {"garage": bool(on)}})


def _setting(key: str, default):
    try:
        return _config().get(key)
    except Exception:
        return default


class Garage:
    def __init__(self, face=None, remote=None, api_key: str = "", sleep_now=None, on_event=None,
                 remember=None, muted=lambda: False):
        self.face, self.remote, self.api_key = face, remote, api_key
        self.sleep_now, self._on_event = sleep_now, on_event or (lambda kind, detail="": None)
        self._told_fallback = False
        self.remember, self.muted = remember, muted
        self.resume = {"handle": ""}
        self.control: dict = {}
        self._stop = threading.Event()
        self.gate = None
        self.last_scores: list[float] = []

    def on_event(self, kind: str, detail: str = "") -> None:
        self._on_event(kind, detail)
        if kind == "gate" and detail == "voice check unavailable: wake word" and not self._told_fallback:
            # Once per garage session: say why he suddenly needs "Hey Willie" again.
            say = self.control.get("say")
            if say:
                self._told_fallback = True
                say("[ROBOT] Zeg kort tegen Wouter: de stemcontrole op de thuisserver werkt nu niet "
                    "(geen stemprofiel of server weg), dus zeg even 'Hey Willie' voor je vraag.")
        elif kind == "gate" and detail == "voice check back":
            self._told_fallback = False

    # ------------------------------------------------------------ the voice check
    def check(self, pcm: bytes) -> bool | None:
        if not self.remote:
            return None
        args = {"pcm": base64.b64encode(pcm).decode("ascii"), "naam": _setting("garage.profile", "wouter")}
        result = self.remote.hub_call("stem_check", args, CHECK_TIMEOUT_S)
        match = result.get("match")
        if match is None:
            log.info("voice check unavailable: %s", result.get("fout"))
            return None
        score = result.get("score")
        if isinstance(score, (int, float)):
            self.last_scores = (self.last_scores + [score])[-20:]
        self.on_event("gate", f"{'Wouter' if match else 'someone else'} ({score})")
        return bool(match)

    def _probe(self) -> None:
        """While the gate runs on the wake word: is the server's voice check back?"""
        while not self._stop.wait(PROBE_EVERY_S):
            gate = self.gate
            if gate is None or gate.available or not self.remote:
                continue
            status = self.remote.hub_call("stem_status", {"naam": _setting("garage.profile", "wouter")}, 5)
            if status.get("klaar"):
                gate.set_available(True)

    # ------------------------------------------------------------ the danger watch
    def _watch(self) -> None:
        next_at = time.monotonic() + 30          # the first look shortly after switching on
        while not self._stop.wait(5):
            every = float(_setting("garage.watch_s", 120) or 0)
            if not every or not self.remote or time.monotonic() < next_at:
                continue
            next_at = time.monotonic() + every
            say = self.control.get("say")
            if not say:
                continue
            try:
                jpeg = self._photo()
            except Exception as exc:
                log.info("danger watch: no photo (%s)", exc)
                continue
            result = self.remote.hub_call("garage_gevaar", {"jpeg": base64.b64encode(jpeg).decode("ascii")}, 45)
            if result.get("gevaar") and result.get("melding"):
                log.warning("danger: %s", result["melding"])
                self.on_event("danger", result["melding"])
                say(f"[ROBOT] Gevaar gezien op je camera: {result['melding']} Waarschuw Wouter nu, kort.")

    def _photo(self) -> bytes:
        from willie.voice import tools
        frame = tools.FRAME_SOURCE() if tools.FRAME_SOURCE else None
        if frame:
            return frame
        import sys
        sys.path.insert(0, str(Path(tools.REPO) / "tools"))
        from ask_camera import capture
        if self.face:
            self.face.indicators(camera=True)
        try:
            with tempfile.TemporaryDirectory(prefix="willie-garage-") as tmp:
                image = Path(tmp) / "garage.jpg"
                capture(image, quiet=True)
                return image.read_bytes()
        finally:
            if self.face:
                self.face.indicators(camera=False)

    # ------------------------------------------------------------ main loop
    def keep_going(self) -> bool:
        return enabled() and not self.muted() and not (self.sleep_now and self.sleep_now.is_set())

    def run(self) -> None:
        from willie.voice import gemini_live, wake
        from willie.voice.gate import VoiceGate

        if _setting("garage.voice_check", "wouter") == "wouter":
            self.gate = VoiceGate(self.check, float(_setting("garage.verify_s", 1.0)),
                                  spotter_factory=wake.Spotter if wake.available() else None,
                                  followup_s=gemini_live.configured_followup())
            if not self.remote:
                self.gate.set_available(False)
        if self.remote:
            self.remote.garage_control = self.control
        if self.face:
            self.face.mode("GARAGE")
        threads = [threading.Thread(target=self._probe, name="garage-probe", daemon=True),
                   threading.Thread(target=self._watch, name="garage-watch", daemon=True)]
        for t in threads:
            t.start()
        failures = 0
        log.info("garage mode on")
        try:
            while self.keep_going():
                transcript: list = []
                started, opened = datetime.now(), time.monotonic()
                if self.remote:
                    self.remote.session_active = True
                try:
                    asyncio.run(gemini_live.session(
                        self.api_key, idle_timeout=None, on_event=self.on_event, face=self.face,
                        cancel=self.sleep_now, transcript=transcript, gate=self.gate, resume=self.resume,
                        extra_prompt=GARAGE_PROMPT, until=self.keep_going, control=self.control))
                    failures = 0
                except RuntimeError as exc:
                    log.warning("garage session failed: %s", exc)
                    if self.resume.get("handle"):
                        self.resume["handle"] = ""           # an expired handle: start fresh
                    time.sleep(RETRY_S[min(failures, len(RETRY_S) - 1)])
                    failures += 1
                finally:
                    if self.remote:
                        self.remote.session_active = False
                    if transcript and self.remember:
                        threading.Thread(target=self.remember, args=(transcript, started), daemon=True).start()
                if time.monotonic() - opened < 5:              # closed straight away: do not spin
                    time.sleep(2)
        finally:
            self._stop.set()
            if self.gate:
                self.gate.close()
            if self.remote:
                self.remote.garage_control = None
            if self.face:
                self.face.mode("")
            log.info("garage mode off")
