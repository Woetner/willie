"""Screen backlight switch (Phase P5): one transistor on a Pi GPIO, high = on.

The clone ILI9486 board has no backlight pin (Wouter, 2 Oct), so until P5's transistor is fitted
`sleep.backlight_gpio` is 0 and deep sleep only makes the screen black. Uses `pinctrl` (Pi OS),
no GPIO library in the face process (D21).
"""
from __future__ import annotations

import logging
import subprocess

log = logging.getLogger("willie.face")


class Backlight:
    def __init__(self):
        self.state = None               # (gpio, on) last written

    def set(self, gpio: int, on: bool) -> None:
        gpio = int(gpio or 0)
        if not gpio or self.state == (gpio, on):
            return
        try:
            subprocess.run(["pinctrl", "set", str(gpio), "op", "dh" if on else "dl"],
                           check=True, capture_output=True, timeout=2)
            self.state = (gpio, on)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("backlight GPIO %d unchanged: %s", gpio, exc)
