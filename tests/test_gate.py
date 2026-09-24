"""K1 garage mode: the voice gate only lets Wouter's utterances through."""
import threading
import time

from willie.voice.gate import END, VoiceGate


def run(gate, pattern, start=0.0, wait_for_check=True):
    """Feed a pattern of chunks: 'S' = speech, '.' = quiet. Returns (sent, now)."""
    sent, now = [], start
    for i, kind in enumerate(pattern):
        chunk = f"{kind}{i}".encode()
        if wait_for_check and gate.pending is not None:
            gate.pending.result(timeout=2)
        sent += gate.feed(chunk, kind == "S", now)
        now += 0.1
    return sent, now


def test_wouter_passes_whole_utterance_then_end():
    gate = VoiceGate(lambda pcm: True, verify_s=0.5)
    sent, _ = run(gate, "..." + "S" * 12 + "." * 16)
    speech = [c for c in sent if c.startswith(b"S")]
    assert len(speech) == 12                          # nothing lost while the check ran
    assert sent[0].startswith(b".")                   # preroll before the first word
    assert sent[-1] == END and sent.count(END) == 1
    assert gate.state == "idle" and gate.stats["passed"] == 1


def test_someone_else_is_dropped():
    gate = VoiceGate(lambda pcm: False, verify_s=0.5)
    sent, _ = run(gate, "S" * 12 + "." * 12)
    assert sent == [] and gate.stats["rejected"] == 1 and gate.state == "idle"


def test_short_utterance_checked_at_its_end():
    seen = []
    gate = VoiceGate(lambda pcm: seen.append(pcm) or True, verify_s=1.0)
    sent, _ = run(gate, "SSSSS" + "." * 20)            # 0.5 s "ja": shorter than verify_s
    assert seen and sum(c.startswith(b"S") for c in sent) == 5
    assert END in sent


def test_click_is_no_turn():
    calls = []
    gate = VoiceGate(lambda pcm: calls.append(1) or True, verify_s=1.0)
    sent, _ = run(gate, "S" + "." * 20)
    assert sent == [] and calls == []


def test_server_down_falls_back_to_wake_word():
    class Spotter:
        def __init__(self):
            self.n = 0

        def feed(self, chunk):
            return chunk.startswith(b"W")

    events = []
    gate = VoiceGate(lambda pcm: None, verify_s=0.3, spotter_factory=Spotter, followup_s=1.0,
                     on_event=lambda k, d="": events.append((k, d)))
    sent, now = run(gate, "SSSS" + "." * 10)
    assert sent == [] and not gate.available
    assert any("unavailable" in d for _, d in events)
    # "Hey Willie" (W) opens the window: the held recent audio goes up, then everything.
    sent = gate.feed(b"W0", True, now)
    assert sent and sent[-1] == b"W0"
    assert gate.feed(b"S1", True, now + 0.1) == [b"S1"]
    assert gate.feed(b"S2", True, now + 5) == []           # window over
    gate.set_available(True)
    assert gate.available


def test_slow_check_keeps_buffering():
    release = threading.Event()
    gate = VoiceGate(lambda pcm: release.wait(2) and True, verify_s=0.3)
    sent, now = run(gate, "S" * 8, wait_for_check=False)
    assert sent == []                                      # still waiting
    release.set()
    time.sleep(0.05)
    sent = gate.feed(b"S9", True, now)
    assert len([c for c in sent if c.startswith(b"S")]) == 9
