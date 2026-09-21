"""The voice adapter interface (D3, D9): one realtime speech-to-speech session, any provider.

Everything above this line (face, mood, audio I/O, tools) talks to a `VoiceAdapter` and never to
a provider's API, so Gate G1 can swap Gemini Live for OpenAI Realtime by changing
`voice.adapter` in willie.yaml.

Audio contract (both directions: 16-bit little-endian mono PCM):
  up    `send_audio()` at `in_rate` (16 kHz), in chunks of any size (100 ms is typical)
  down  the `on_audio` callback at `out_rate`; the adapter does not play it

Lifecycle:
    adapter.on_audio(play); adapter.on_tool_call(run_tool); adapter.on_event(face_state)
    await adapter.start_session(persona, context, tools)
    ... await adapter.send_audio(chunk) as the microphone delivers ...
    await adapter.interrupt()          # local barge-in, or the user said "stop"
    await adapter.close()

Events for the face and the mood engine (`on_event(kind, detail)`):
  ready          session is open (detail: model name)
  speaking       first audio of a model turn
  interrupted    the model's turn was cut off (by the user's voice or interrupt())
  turn_complete  the model finished its turn
  tool           a tool call is about to run (detail: name)
  error          something failed; the session may still be open
  closed         the session ended (detail: reason)
"""
from __future__ import annotations

import abc
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger("willie.voice")

AudioFn = Callable[[bytes], Any]
ToolFn = Callable[[str, dict], "dict | Awaitable[dict]"]
EventFn = Callable[[str, str], Any]


@dataclass
class Tool:
    """A function the model may call. `parameters` is a JSON Schema object."""
    name: str
    description: str
    parameters: dict = field(default_factory=lambda: {"type": "object", "properties": {}})
    handler: ToolFn | None = None        # used when no on_tool_call callback is set

    def declaration(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


class VoiceAdapter(abc.ABC):
    name = "base"
    in_rate = 16_000
    out_rate = 24_000

    def __init__(self) -> None:
        self._audio_fn: AudioFn | None = None
        self._tool_fn: ToolFn | None = None
        self._event_fn: EventFn | None = None
        self.tools: dict[str, Tool] = {}
        self.is_open = False

    # ---- callbacks (set before start_session) --------------------------------
    def on_audio(self, fn: AudioFn) -> None:
        self._audio_fn = fn

    def on_tool_call(self, fn: ToolFn) -> None:
        self._tool_fn = fn

    def on_event(self, fn: EventFn) -> None:
        self._event_fn = fn

    # ---- what every adapter implements ---------------------------------------
    @abc.abstractmethod
    async def start_session(self, persona: str, context: str, tools: list[Tool]) -> None:
        """Open the session. `persona` = config/persona.md, `context` = the live block
        (time, mood, battery, place) that goes after it in the system prompt."""

    @abc.abstractmethod
    async def send_audio(self, pcm: bytes) -> None:
        """Microphone audio up, at `in_rate`."""

    @abc.abstractmethod
    async def interrupt(self) -> None:
        """Stop the model's current turn now; later audio from that turn is dropped."""

    @abc.abstractmethod
    async def close(self) -> None:
        """End the session and release everything. Safe to call twice."""

    # ---- helpers for implementations -----------------------------------------
    def _register_tools(self, tools: list[Tool]) -> None:
        self.tools = {t.name: t for t in tools}

    async def _emit(self, kind: str, detail: str = "") -> None:
        if self._event_fn:
            await _maybe_await(self._event_fn(kind, detail))

    async def _deliver_audio(self, pcm: bytes) -> None:
        if self._audio_fn:
            await _maybe_await(self._audio_fn(pcm))

    async def _run_tool(self, name: str, args: dict) -> dict:
        """Run one tool call; never raises (the model gets an error result instead)."""
        await self._emit("tool", name)
        try:
            if self._tool_fn:
                return await _maybe_await(self._tool_fn(name, args))
            tool = self.tools.get(name)
            if tool is None or tool.handler is None:
                return {"error": f"unknown tool {name}"}
            return await _maybe_await(tool.handler(args))
        except Exception as e:                       # a broken tool must not end the conversation
            log.exception("tool %s failed", name)
            await self._emit("error", f"tool {name}: {e}")
            return {"error": f"{type(e).__name__}: {e}"}


async def _maybe_await(value):
    return await value if inspect.isawaitable(value) else value


def create(name: str, **kwargs) -> VoiceAdapter:
    """`voice.adapter` from willie.yaml -> an adapter instance."""
    if name == "fake":
        from willie.voice.fake import FakeAdapter
        return FakeAdapter(**kwargs)
    raise ValueError(f"no voice adapter {name!r} yet (D4: gemini_live, D5: openai_realtime)")
