"""A voice adapter that needs no network (D3): for tests, the face (D6) and run-local.

It behaves like a realtime model with server-side voice detection:
- speech = audio chunks with a peak above `speech_level`; a turn ends after `end_ms` of silence
- then it answers: optionally calls the next scripted tool, then streams a tone in real time
- loud audio while it is answering counts as barge-in and cuts the answer off
"""
from __future__ import annotations

import array
import asyncio
import math

from willie.voice.base import Tool, VoiceAdapter


def tone(ms: int, rate: int, hz: float = 440.0, level: float = 0.3) -> bytes:
    n = rate * ms // 1000
    return array.array("h", (int(level * 32767 * math.sin(2 * math.pi * hz * i / rate)) for i in range(n))).tobytes()


def peak(pcm: bytes) -> float:
    s = array.array("h")
    s.frombytes(pcm[: len(pcm) // 2 * 2])
    return max((abs(v) for v in s), default=0) / 32768


class FakeAdapter(VoiceAdapter):
    name = "fake"

    def __init__(self, script: list[dict] | None = None, answer_ms: int = 600, chunk_ms: int = 40,
                 speech_level: float = 0.05, end_ms: int = 300, realtime: bool = True):
        """`script`: one dict per turn, e.g. {"tool": "show", "args": {"text": "hi"}}; turns past
        the end of the script just answer."""
        super().__init__()
        self.script = list(script or [])
        self.answer_ms, self.chunk_ms, self.realtime = answer_ms, chunk_ms, realtime
        self.speech_level, self.end_ms = speech_level, end_ms
        self.system_prompt = ""
        self.tool_results: list[tuple[str, dict]] = []
        self.turns = 0
        self._heard_ms = 0.0                # speech in the current user turn
        self._silence_ms = 0.0
        self._answer: asyncio.Task | None = None

    async def start_session(self, persona: str, context: str, tools: list[Tool]) -> None:
        self._register_tools(tools)
        self.system_prompt = f"{persona}\n\n{context}".strip()
        self.is_open = True
        await self._emit("ready", "fake")

    async def send_audio(self, pcm: bytes) -> None:
        if not self.is_open:
            raise ConnectionError("session closed")
        ms = len(pcm) / 2 / self.in_rate * 1000
        loud = peak(pcm) >= self.speech_level
        if self.answering and loud:                  # barge-in, like a server VAD
            await self.interrupt()
        if loud:
            self._heard_ms += ms
            self._silence_ms = 0
        elif self._heard_ms:
            self._silence_ms += ms
            if self._silence_ms >= self.end_ms:      # end of the user's turn -> answer
                self._heard_ms = self._silence_ms = 0
                self._answer = asyncio.create_task(self._respond())

    @property
    def answering(self) -> bool:
        return self._answer is not None and not self._answer.done()

    async def _respond(self) -> None:
        turn = self.script[self.turns] if self.turns < len(self.script) else {}
        self.turns += 1
        if turn.get("tool"):
            result = await self._run_tool(turn["tool"], turn.get("args", {}))
            self.tool_results.append((turn["tool"], result))
        pcm = tone(self.answer_ms, self.out_rate)
        step = self.out_rate * self.chunk_ms // 1000 * 2
        for i in range(0, len(pcm), step):
            if i == 0:
                await self._emit("speaking")
            await self._deliver_audio(pcm[i:i + step])
            await asyncio.sleep(self.chunk_ms / 1000 if self.realtime else 0)
        await self._emit("turn_complete")

    async def interrupt(self) -> None:
        if self.answering:
            self._answer.cancel()
            try:
                await self._answer
            except asyncio.CancelledError:
                pass
            await self._emit("interrupted")

    async def close(self) -> None:
        if not self.is_open:
            return
        await self.interrupt()
        self.is_open = False
        await self._emit("closed", "close()")
