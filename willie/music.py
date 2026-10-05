"""Music router (U5b): Spotify and radio behind one object, for the voice loop and the speaker.

Both sources share the one sound card and the same handover with his voice (TALKING /
pause_for_talk / after_talk / hold / stop). This module forwards each call to both - a source
that is not playing does nothing - so the voice loop does not care which one is on. It also
watches the `music.source` setting: change it in the dashboard or app while something plays
and the other source takes over.
"""
from __future__ import annotations

import logging
from contextlib import ExitStack, contextmanager

from willie.skills import radio, spotify

log = logging.getLogger("willie.music")

_SOURCES = (spotify, radio)


def talking(on: bool = True) -> None:
    """The conversation holds the sound card (set at once on the wake word)."""
    for source in _SOURCES:
        source.TALKING = on


def pause_for_talk() -> None:
    for source in _SOURCES:
        source.pause_for_talk()


def after_talk(resume: bool = True) -> None:
    for source in _SOURCES:
        source.after_talk(resume)


@contextmanager
def hold():
    with ExitStack() as stack:
        for source in _SOURCES:
            stack.enter_context(source.hold())
        yield


def stop() -> None:
    for source in _SOURCES:
        source.stop()


def playing() -> bool:
    return any(source.playing() for source in _SOURCES)


def card_busy() -> bool:
    return any(source.card_busy() for source in _SOURCES)


def now_playing() -> str:
    return radio.now_playing() or spotify.now_playing()


def sync() -> None:
    """The setting says one source, the other one still plays: switch over. Cheap (local state only)."""
    radio.refresh_sound()
    if spotify.TALKING or radio.TALKING or radio.SWITCH.locked():
        return
    wanted = radio.source()
    if wanted == "radio" and spotify.playing():
        log.info("source changed to radio while Spotify plays")
        radio.speel_radio("")
    elif wanted == "spotify" and radio.playing():
        log.info("source changed to Spotify while the radio plays")
        spotify.speel_muziek()
