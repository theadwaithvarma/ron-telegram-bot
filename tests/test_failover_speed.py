#!/usr/bin/env python3
"""Offline tests for ron_ai's 429 failover latency fix (no network).

2026-09-22 (speed): a 429 used to sleep up to 30s ("honor the try-again
wait") before failing over — the dominant "Ron is slow" latency when the
free-tier providers at the front of the chain rate-limited. Now a 429
cools the provider down and fails over IMMEDIATELY, with no sleep.

Verifies: no long sleep on 429 (short or long retry-after alike), the
next provider is tried right away, the 429'd provider still gets a
cooldown (no hammering — safety preserved), and all-429 still raises
the provider-neutral RateLimitedError (degraded-mode path intact).
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import importlib.util as _ilu

_spec = _ilu.spec_from_file_location(
    "ron_ai_under_test",
    os.path.join(os.path.dirname(__file__), "..", "ron_ai.py"))
ron_ai = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(ron_ai)


class FakeStreamResp:
    def __init__(self, status, body=b""):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def close(self):
        pass


class FakePool:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def stream(self, method, path, body, headers, timeout):
        model = json.loads(body.decode())["model"]
        self.calls.append(model)
        return self.handler(model)


CFG = {"providers": [
    {"name": "openrouter", "models": ["or-slow-free"]},
    {"name": "groq", "models": ["groq-fast"]},
]}


def _handler_429_then_200(retry_after_body):
    def handler(model):
        if model == "or-slow-free":
            return FakeStreamResp(429, retry_after_body)
        if model == "groq-fast":
            return FakeStreamResp(200, b"{}")
        raise AssertionError(f"unexpected model {model}")
    return handler


class FailoverSpeedTestBase(unittest.TestCase):
    def setUp(self):
        ron_ai._provider_cooldown.clear()
        ron_ai._provider_5xx_streak.clear()
        self.attempts = []
        ron_ai.AI_CALL_HOOK = self.attempts.append
        self._patches = [
            mock.patch.object(ron_ai, "_provider_key",
                              return_value="fake-key"),
            mock.patch.object(ron_ai.time, "sleep"),
        ]
        for p in self._patches:
            p.start()
        # ron_ai.time.sleep is the mock; keep a handle to inspect calls.
        self.sleep_mock = ron_ai.time.sleep

    def tearDown(self):
        for p in self._patches:
            p.stop()
        ron_ai.AI_CALL_HOOK = None
        ron_ai._provider_cooldown.clear()
        ron_ai._provider_5xx_streak.clear()

    def _run(self, cfg, handler):
        pool = FakePool(handler)
        with mock.patch.object(ron_ai.http_pool, "pool_for",
                               return_value=pool):
            with redirect_stdout(io.StringIO()):
                resp = ron_ai._request(cfg, [{"role": "user",
                                             "content": "hi"}],
                                      stream=False, timeout=25)
        resp.close()
        return pool


class Test429FailoverSpeed(FailoverSpeedTestBase):
    def test_short_retry_after_no_sleep(self):
        """429 with 'try again in 8s' must NOT sleep — fail over at once."""
        pool = self._run(
            CFG, _handler_429_then_200(b'{"error": "try again in 8s"}'))
        self.assertEqual(pool.calls, ["or-slow-free", "groq-fast"])
        self.assertEqual(self.attempts, ["openrouter", "groq"])
        for c in self.sleep_mock.call_args_list:
            self.assertLess(c.args[0], 5.0,
                            f"429 path slept too long: {c.args[0]}s")

    def test_long_retry_after_no_sleep(self):
        """429 with 'try again in 45s' must NOT sleep 30s — fail over."""
        pool = self._run(
            CFG, _handler_429_then_200(b'{"error": "try again in 45s"}'))
        self.assertEqual(pool.calls, ["or-slow-free", "groq-fast"])
        for c in self.sleep_mock.call_args_list:
            self.assertLess(c.args[0], 5.0,
                            f"429 path slept too long: {c.args[0]}s")

    def test_429_still_cools_down_provider(self):
        """Safety preserved: the 429'd provider is cooled down (no hammer)."""
        self._run(CFG, _handler_429_then_200(b'{"error": "try again in 8s"}'))
        self.assertIn("openrouter", ron_ai._provider_cooldown)
        # Next call skips the cooled provider entirely — straight to groq.
        self.attempts.clear()
        pool = self._run(
            CFG, _handler_429_then_200(b'{"error": "try again in 8s"}'))
        self.assertEqual(pool.calls, ["groq-fast"])
        self.assertEqual(self.attempts, ["groq"])

    def test_all_429_raises_rate_limited(self):
        """All providers 429 -> provider-neutral RateLimitedError, no sleep."""
        def handler(model):
            return FakeStreamResp(429, b'{"error": "try again in 12s"}')
        with mock.patch.object(ron_ai.http_pool, "pool_for",
                               return_value=FakePool(handler)):
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(ron_ai.RateLimitedError) as ctx:
                    ron_ai._request(CFG, [{"role": "user", "content": "hi"}],
                                    stream=False, timeout=25)
        self.assertIn("rate limited", str(ctx.exception))
        for c in self.sleep_mock.call_args_list:
            self.assertLess(c.args[0], 5.0,
                            f"429 path slept too long: {c.args[0]}s")


if __name__ == "__main__":
    unittest.main()
