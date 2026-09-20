from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import ask_voice  # noqa: E402


class VoiceRoutingTests(unittest.TestCase):
    def test_understand_parses_wake_and_general_answer(self) -> None:
        payload = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": (
                                    '{"wake_detected":true,"has_question":true,'
                                    '"needs_camera":false,"answer":"Vier."}'
                                )
                            }
                        ]
                    }
                }
            ]
        }
        with mock.patch.object(ask_voice, "_gemini", return_value=payload) as request:
            decision = ask_voice.understand(b"wav", "secret", wake_required=True)

        self.assertTrue(decision.wake_detected)
        self.assertTrue(decision.has_question)
        self.assertFalse(decision.needs_camera)
        self.assertEqual(decision.answer, "Vier.")
        self.assertIn("wake_required: true", request.call_args.args[1])

    def test_auto_mode_skips_camera_and_second_request(self) -> None:
        decision = ask_voice.VoiceDecision(True, True, False, "Audio-only answer")
        with mock.patch.object(ask_voice, "capture") as capture, mock.patch.object(ask_voice, "ask") as ask:
            result = ask_voice.answer(b"wav", "secret", decision=decision)

        self.assertEqual(result, ("Audio-only answer", False))
        capture.assert_not_called()
        ask.assert_not_called()

    def test_auto_mode_captures_when_vision_is_needed(self) -> None:
        decision = ask_voice.VoiceDecision(True, True, True, "")

        def fake_capture(path: Path, quiet: bool) -> None:
            self.assertTrue(quiet)
            path.write_bytes(b"jpeg")

        with tempfile.TemporaryDirectory() as directory:
            saved = Path(directory) / "view.jpg"
            with mock.patch.object(ask_voice, "capture", side_effect=fake_capture) as capture, mock.patch.object(
                ask_voice, "ask", return_value="I can see it."
            ) as ask:
                result = ask_voice.answer(b"wav", "secret", decision=decision, save=saved)

        self.assertEqual(result, ("I can see it.", True))
        capture.assert_called_once()
        self.assertIsNotNone(ask.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
