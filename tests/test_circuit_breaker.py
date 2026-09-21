#!/usr/bin/env python3
"""Offline tests for ron_ai's 5xx circuit breaker (no network).

Simulates a provider 503ing and verifies: the breaker trips after
BREAKER_TRIP_STREAK consecutive 5xx, a tripped provider is skipped on
later calls (no more failover burn), a 200 resets the streak, the
cooldown expiring lets the provider be tried again, and 429s don't
feed the 5xx streak.
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ron_daemon.py inserts the LIVE dir (~/workspace/telegram-bot) at
# sys.path[0]; test modules that import it first (e.g. test_active) leave
# the live dir's stale ron_ai in sys.modules. Evict it so the import
# below resolves to this repo's copy.
for _mod in ("ron_ai", "http_pool"):
    sys.modules.pop(_mod, None)

import ron_ai


class FakeStreamResp:
    def __init__(self, status, body=b""):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def close(self):
        pass


class FakePool:
    """Routes by the model name inside the JSON request body."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def stream(self, method, path, body, headers, timeout):
        model = json.loads(body.decode())["model"]
        self.calls.append(model)
        return self.handler(model)


CFG = {"providers": [
    {"name": "gemini", "models": ["gemini-sick-m1", "gemini-sick-m2"]},
    {"name": "groq", "models": ["groq-healthy-m1"]},
]}


def _resp_for(sick_models, healthy_models, sick_status=503):
    def handler(model):
        if model in sick_models:
            return FakeStreamResp(sick_status, b'{"error": "overloaded"}')
        if model in healthy_models:
            return FakeStreamResp(200, b"{}")
        raise AssertionError(f"unexpected model {model}")
    return handler


class BreakerTestBase(unittest.TestCase):
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


class TestCircuitBreaker(BreakerTestBase):
    def test_trips_after_consecutive_5xx(self):
        pool = self._run(
            CFG, _resp_for({"gemini-sick-m1", "gemini-sick-m2"},
                           {"groq-healthy-m1"}))
        # 2 sick models x (try + 1 retry) = 4 attempts, then healthy.
        self.assertEqual(self.attempts,
                         ["gemini"] * 4 + ["groq"])
        self.assertEqual(pool.calls,
                         ["gemini-sick-m1"] * 2 + ["gemini-sick-m2"] * 2
                         + ["groq-healthy-m1"])
        # Breaker tripped: cooldown entry written for the sick provider.
        self.assertIn("gemini", ron_ai._provider_cooldown)

    def test_tripped_provider_skipped_on_next_call(self):
        self._run(CFG, _resp_for({"gemini-sick-m1", "gemini-sick-m2"},
                                 {"groq-healthy-m1"}))
        self.assertIn("gemini", ron_ai._provider_cooldown)
        self.attempts.clear()
        # Second call: sick provider skipped entirely — straight to groq.
        # (This is the failover-burn fix: no more wasted 503 attempts.)
        pool = self._run(
            CFG, _resp_for({"gemini-sick-m1", "gemini-sick-m2"},
                           {"groq-healthy-m1"}))
        self.assertEqual(self.attempts, ["groq"])
        self.assertEqual(pool.calls, ["groq-healthy-m1"])

    def test_success_resets_streak(self):
        # Scripted: 503 once, then 200 — streak must not survive.
        calls = {"n": 0}

        def handler(model):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeStreamResp(503, b"{}")
            return FakeStreamResp(200, b"{}")

        one = {"providers": [{"name": "groq",
                              "models": ["groq-healthy-m1"]}]}
        self._run(one, handler)   # 503 -> retry -> 200
        self.assertNotIn("groq", ron_ai._provider_cooldown)
        self.assertEqual(ron_ai._provider_5xx_streak.get("groq", 0), 0)

    def test_cooldown_expiry_retries_provider(self):
        self._run(CFG, _resp_for({"gemini-sick-m1", "gemini-sick-m2"},
                                 {"groq-healthy-m1"}))
        self.assertIn("gemini", ron_ai._provider_cooldown)
        # Fast-forward past the breaker cooldown.
        ron_ai._provider_cooldown["gemini"] = (
            ron_ai.time.monotonic() - 1.0)
        self.attempts.clear()
        pool = self._run(
            CFG, _resp_for({"gemini-sick-m1", "gemini-sick-m2"},
                           {"groq-healthy-m1"}))
        # Sick provider tried again (still sick -> re-trips).
        self.assertIn("gemini", self.attempts)
        self.assertIn("gemini", ron_ai._provider_cooldown)

    def test_429_does_not_feed_5xx_streak(self):
        pool = self._run(
            CFG, _resp_for({"gemini-sick-m1", "gemini-sick-m2"},
                           {"groq-healthy-m1"}, sick_status=429))
        # 429 path sets its own cooldown; the 5xx streak stays empty and
        # no breaker trip is logged for it.
        self.assertEqual(dict(ron_ai._provider_5xx_streak), {})
        self.assertIn("gemini", ron_ai._provider_cooldown)  # 429 cooldown
        self.assertEqual(self.attempts[-1], "groq")  # failed over fine

    def test_healthy_provider_unaffected(self):
        # All healthy: no streaks, no cooldowns, first provider wins.
        pool = self._run(CFG, _resp_for(set(), {"gemini-sick-m1",
                                                "gemini-sick-m2",
                                                "groq-healthy-m1"}))
        self.assertEqual(self.attempts, ["gemini"])
        self.assertEqual(dict(ron_ai._provider_cooldown), {})
        self.assertEqual(dict(ron_ai._provider_5xx_streak), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
