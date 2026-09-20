#!/usr/bin/env python3
"""Offline tests for ron_ai.single_shot (no network).

Pinned provider/model plan, message pass-through, no-key and
unknown-provider errors, empty-reply error.
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_ai


class TestSingleShot(unittest.TestCase):
    def test_pins_provider_and_model(self):
        seen = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {"choices": [{"message": {"content": "verd: true"}}]}
                ).encode()

        def fake_request(cfg, messages, stream, timeout, cheap=False):
            seen["cfg"] = cfg
            seen["messages"] = messages
            seen["stream"] = stream
            return FakeResp()

        with mock.patch.object(ron_ai, "_provider_key",
                               return_value="hsurr:fake"), \
             mock.patch.object(ron_ai, "_request", fake_request):
            out = ron_ai.single_shot(
                "perplexity", "sonar-pro",
                [{"role": "user", "content": "hi"}])
        self.assertEqual(out, "verd: true")
        self.assertEqual(seen["cfg"]["providers"],
                         [{"name": "perplexity", "models": ["sonar-pro"]}])
        self.assertEqual(seen["messages"],
                         [{"role": "user", "content": "hi"}])
        self.assertFalse(seen["stream"])

    def test_no_key_raises(self):
        with mock.patch.object(ron_ai, "_provider_key",
                               return_value=None):
            with self.assertRaisesRegex(RuntimeError, "not connected"):
                ron_ai.single_shot("perplexity", "sonar-pro", [])

    def test_unknown_provider_raises(self):
        with self.assertRaisesRegex(RuntimeError, "unknown AI provider"):
            ron_ai.single_shot("nope", "m", [])

    def test_empty_reply_raises(self):
        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {"choices": [{"message": {"content": "  "}}]}).encode()

        with mock.patch.object(ron_ai, "_provider_key",
                               return_value="hsurr:fake"), \
             mock.patch.object(ron_ai, "_request",
                               return_value=FakeResp()):
            with self.assertRaisesRegex(RuntimeError, "empty reply"):
                ron_ai.single_shot("gemini", "gemini-3.8-flash", [])



if __name__ == "__main__":
    unittest.main()
