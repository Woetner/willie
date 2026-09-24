"""Search mode (K3): "zoek de kat / mijn schroevendraaier / mama".

Order (Wouter, 24 Sep): memory first, then a sweep of the house, then the eufy cameras.

1. memory: where was it seen last (hub `waar_is`) - said at once, the search goes on
2. sweep: at every stop a head scan (5 directions); each photo goes to the server
   (`missie_kijk`: known thing/person by its prints, else CLIP against the English
   description). Between stops he turns and drives a leg, obstacles stop the leg (F6).
   Until the K4 map exists this is a local spiral, not yet room by room.
3. eufy: people and animals may be outside (`zoek_buiten`)

Stops at: found, `search.max_min`, battery low, or "stop".
"""
from __future__ import annotations

import base64
import logging
import time

from . import Mission

log = logging.getLogger("willie.missions.search")

PANS = (-80, -40, 0, 40, 80)
LEG_M = 0.8
TURN_DEG = 110                 # not a multiple of 90: the spiral does not retrace itself
MAX_STOPS = 12


def _setting(key: str, default):
    try:
        from willie.config import Config
        return Config().get(key)
    except Exception:
        return default


class Search(Mission):
    kind, label = "zoek", "SEARCH"

    def run(self) -> dict:
        name = str(self.args.get("naam") or "").strip()
        text_en = str(self.args.get("omschrijving_en") or "").strip()
        if not name and not text_en:
            return {"fout": "wat moet ik zoeken?"}
        what = name or text_en
        max_s = float(_setting("search.max_min", 10)) * 60
        deadline = time.monotonic() + max_s
        self.show("seeing", f"ZOEK: {what}"[:40])

        memory = self.robot.hub("waar_is", {"naam": name}, 10) if name else {}
        last = (memory.get("laatst") or [{}])[0] if isinstance(memory.get("laatst"), list) else {}
        if last:
            self.progress = f"laatst gezien {last.get('wanneer')} ({last.get('waar')})"

        stops = 0
        while not self.stopped and time.monotonic() < deadline and stops < MAX_STOPS:
            if self.battery_low():
                return self._report(False, what, "batterij bijna leeg", last)
            for pan in PANS:
                if self.stopped:
                    break
                self.look(pan)
                jpeg = self.robot.photo()
                if not jpeg:
                    continue
                seen = self.robot.hub("missie_kijk", {"jpeg": base64.b64encode(jpeg).decode("ascii"),
                                                      "naam": name, "omschrijving_en": text_en}, 30)
                if seen.get("gevonden"):
                    self.show("surprised", f"GEVONDEN: {what}"[:40])
                    self.robot.hub("missie_melding", {"tekst": f"Gevonden: {what}", "jpeg":
                                   base64.b64encode(jpeg).decode("ascii")}, 20)
                    self.robot.say(f"Gevonden: {what}!")
                    return self._report(True, what, f"na {stops} stops, hoofd {pan}°", last, seen.get("zeker"))
            stops += 1
            self.progress = f"{stops} plekken bekeken"
            self.look(0, settle=0.2)
            self.robot.body("turn", deg=TURN_DEG)
            moved = self.robot.body("move", m=LEG_M)
            if not moved.get("ok") and "garagemodus" in str(moved.get("reden", "")):
                return self._report(False, what, "ik mag niet rijden (garagemodus)", last)
        if self.stopped:
            return self._report(False, what, "gestopt", last)
        outside = self.robot.hub("zoek_buiten", {"naam": name, "omschrijving_en": text_en}, 90)
        if outside.get("gevonden"):
            self.robot.say(f"Ik zie {what} op de camera {outside.get('camera')}.")
            return self._report(True, what, f"buitencamera {outside.get('camera')}", last, outside.get("zeker"))
        self.robot.say(f"Ik heb {what} niet gevonden.")
        return self._report(False, what, f"{stops} plekken en de buitencamera's bekeken", last)

    def _report(self, found: bool, what: str, how: str, last: dict, score=None) -> dict:
        out = {"gevonden": found, "wat": what, "hoe": how}
        if score is not None:
            out["zeker"] = score
        if last:
            out["laatst_gezien"] = f"{last.get('wanneer')} - {last.get('waar')}"
        self.progress = ("gevonden: " if found else "niet gevonden: ") + how
        return out
