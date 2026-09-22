#!/usr/bin/env python3
"""Offline tests for the Stop-button UX (no network).

When the user hits Telegram's native Stop before the first token
arrives, chat_stream/vision_stream must raise ron_ai.UserStoppedError
— NOT "AI backend returned empty reply" — so the daemon skips the
single-send fallback instead of re-running the full AI chain after an
intentional stop. A genuinely empty stream (no stop) still raises the
plain RuntimeError, and a stop mid-stream keeps the partial text.
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Load THIS repo's ron_ai under a distinct module name. Rationale:
# ron_daemon.py inserts the LIVE dir (~/workspace/telegram-bot) at
# sys.path[0], so in a full `unittest discover` run sys.modules['ron_ai']
# is the live dir's copy (imported first via test_active -> ron_daemon).
# Evicting/re-importing it would perturb every other test module that
# patches ron_ai attributes (e.g. test_vision) while the daemon still
# holds the old copy. A distinct name keeps this file hermetic.
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location(
    "ron_ai_under_test",
    os.path.join(os.path.dirname(__file__), "..", "ron_ai.py"))
ron_ai = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(ron_ai)


class FakeStreamResp:
    """Mimics the SSE readline() protocol _drain_stream uses."""

    def __init__(self, lines):
        self._lines = list(lines)

    def readline(self):
        return self._lines.pop(0) if self._lines else b""

    def close(self):
        pass


def _sse(*contents):
    lines = []
    for c in contents:
        lines.append(b'data: {"choices": [{"delta": {"content": "'
                     + c.encode() + b'"}}]}')
    lines.append(b"data: [DONE]")
    return lines


class StopTestBase(unittest.TestCase):
    def setUp(self):
        self._orig_request = ron_ai._request
        self._orig_save = ron_ai._save_turn
        ron_ai._save_turn = lambda *a, **k: None

    def tearDown(self):
        ron_ai._request = self._orig_request
        ron_ai._save_turn = self._orig_save

    def _fake_request(self, lines):
        def fake(cfg, messages, stream, timeout, tier="standard"):
            return FakeStreamResp(lines)
        ron_ai._request = fake


class TestUserStop(StopTestBase):
    def test_stop_with_no_tokens_raises_user_stopped(self):
        self._fake_request(_sse())
        ev = threading.Event()
        ev.set()  # user hit Stop during the Thinking phase
        with self.assertRaises(ron_ai.UserStoppedError):
            ron_ai.chat_stream("STOP1", "hello", "Adwaith",
                               stop_event=ev)

    def test_empty_stream_without_stop_raises_empty_reply(self):
        self._fake_request(_sse())
        with self.assertRaisesRegex(RuntimeError,
                                    "AI backend returned empty reply") as cm:
            ron_ai.chat_stream("STOP2", "hello", "Adwaith",
                               stop_event=threading.Event())
        self.assertNotIsInstance(cm.exception, ron_ai.UserStoppedError)

    def test_stop_with_partial_content_returns_partial(self):
        self._fake_request(_sse("hel", "lo"))
        ev = threading.Event()

        def on_token(tok):
            ev.set()  # user hits Stop as soon as text starts flowing

        reply = ron_ai.chat_stream("STOP3", "hello", "Adwaith",
                                   on_token=on_token, stop_event=ev)
        self.assertEqual(reply, "hel")

    def test_user_stopped_is_runtime_error(self):
        # Backward compatible: generic handlers still catch it.
        self.assertTrue(issubclass(ron_ai.UserStoppedError,
                                   RuntimeError))

    def test_vision_stop_with_no_tokens_raises_user_stopped(self):
        self._fake_request(_sse())
        ev = threading.Event()
        ev.set()
        with self.assertRaises(ron_ai.UserStoppedError):
            ron_ai.vision_stream("aGVsbG8=", "what is this?",
                                 stop_event=ev)

    def test_vision_empty_stream_without_stop_raises_empty_reply(self):
        self._fake_request(_sse())
        with self.assertRaisesRegex(RuntimeError,
                                    "AI backend returned empty reply") as cm:
            ron_ai.vision_stream("aGVsbG8=", "what is this?",
                                 stop_event=threading.Event())
        self.assertNotIsInstance(cm.exception, ron_ai.UserStoppedError)


if __name__ == "__main__":
    unittest.main(verbosity=2)
