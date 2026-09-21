#!/usr/bin/env python3
"""Public-bot identity tests: real @username in prompt, no profiling.

- _who_line: valid handle -> identity line; missing/invalid -> "".
- chat()/chat_stream() inject the who-line so the model never invents
  handles like "@Adwaith" for @TheAdwaith.
- System prompt carries the no-profiling rule and the drill-down-only
  button rule.

Run: python3 tests/test_who_line.py  (from ~/workspace/ron-telegram-bot)
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_ai


class FakeResp:
    """Mimics the context-manager + read() protocol chat() uses."""

    def __init__(self, content="ok"):
        self._data = json.dumps(
            {"choices": [{"message": {"content": content}}]}).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeStreamResp:
    """Mimics the SSE readline() protocol _drain_stream uses."""

    def __init__(self, content="ok"):
        self._lines = [
            b'data: {"choices": [{"delta": {"content": "' + content.encode()
            + b'"}}]}',
            b"data: [DONE]",
        ]

    def readline(self):
        return self._lines.pop(0) if self._lines else b""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class WhoLineTest(unittest.TestCase):
    def test_valid_username(self):
        line = ron_ai._who_line("Adwaith", "TheAdwaith")
        self.assertIn("@TheAdwaith", line)
        self.assertIn("never shorten, guess, or invent", line)

    def test_missing_username(self):
        self.assertEqual(ron_ai._who_line("Adwaith", ""), "")
        self.assertEqual(ron_ai._who_line("Adwaith", None), "")

    def test_invalid_username_dropped(self):
        for bad in ("ab", "@evil", "a b", "x" * 33, "adwaith!"):
            self.assertEqual(ron_ai._who_line("Adwaith", bad), "",
                             f"should drop {bad!r}")


class PromptRulesTest(unittest.TestCase):
    def _check(self, system, label):
        self.assertIn("Never profile the", system, label)
        self.assertIn("every user is a stranger", system, label)
        self.assertIn("drill deeper into something this reply already said",
                      system, label)
        self.assertIn("never a new topic", system, label)
        self.assertNotIn("apparent interests", system, label)

    def test_runtime_config_has_rules(self):
        # load_config() reads the LIVE ron_ai.json — this is the prompt
        # the bot actually runs with.
        self._check(ron_ai.load_config()["system"], "live ron_ai.json")

    def test_repo_default_has_rules(self):
        # DEFAULT_CONFIG is the fallback and the documented source of
        # truth in the repo — keep it in sync with the live json.
        self._check(ron_ai.DEFAULT_CONFIG["system"], "DEFAULT_CONFIG")


class ChatUsernameTest(unittest.TestCase):
    def setUp(self):
        self._orig = ron_ai._request
        self.captured = {}

        def fake(cfg, messages, stream, timeout, cheap=False):
            self.captured["messages"] = messages
            return FakeResp("ok")
        ron_ai._request = fake

    def tearDown(self):
        ron_ai._request = self._orig

    def _system(self):
        return self.captured["messages"][0]["content"]

    def test_chat_injects_who_line(self):
        ron_ai.chat("W1", "ping me in the playground group",
                    user_name="Adwaith", username="TheAdwaith")
        self.assertIn("@TheAdwaith", self._system())

    def test_chat_without_username_has_no_who_line(self):
        ron_ai.chat("W2", "hello", user_name="Adwaith")
        self.assertNotIn("Telegram handle @", self._system())

    def test_chat_stream_injects_who_line(self):
        orig = ron_ai._request

        def fake_stream(cfg, messages, stream, timeout, cheap=False):
            self.captured["messages"] = messages
            return FakeStreamResp("ok")
        ron_ai._request = fake_stream
        try:
            seen = []
            ron_ai.chat_stream("W3", "ping me", "Adwaith",
                               on_token=seen.append,
                               username="TheAdwaith")
        finally:
            ron_ai._request = orig
        self.assertIn("@TheAdwaith", self._system())


if __name__ == "__main__":
    unittest.main()
