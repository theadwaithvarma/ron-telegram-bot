#!/usr/bin/env python3
"""/ping latency tests: measures a real API round-trip, reports ms honestly.

Run: python3 tests/test_ping.py   (from ~/workspace/ron-telegram-bot)
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_daemon


class MeasurePingTest(unittest.TestCase):
    def setUp(self):
        self._orig_api = ron_daemon.api
        # EMA is module-global: reset so each test starts with no data.
        self._orig_ema = ron_daemon._API_LAT_EMA
        self._orig_ts = ron_daemon._API_LAT_TS
        ron_daemon._API_LAT_EMA = None
        ron_daemon._API_LAT_TS = 0.0

    def tearDown(self):
        ron_daemon.api = self._orig_api
        ron_daemon._API_LAT_EMA = self._orig_ema
        ron_daemon._API_LAT_TS = self._orig_ts

    def test_success_returns_nonnegative_ms(self):
        def fake_api(method, params=None, timeout=65):
            self.assertEqual(method, "getMe")
            time.sleep(0.01)
            return {"ok": True, "result": {"is_bot": True}}
        ron_daemon.api = fake_api
        ms, err = ron_daemon._measure_ping()
        self.assertIsNone(err)
        self.assertIsInstance(ms, int)
        self.assertGreaterEqual(ms, 10)  # at least the 10ms sleep

    def test_api_error_reports_unreachable(self):
        ron_daemon.api = lambda *a, **k: {"ok": False, "error": "boom"}
        ms, err = ron_daemon._measure_ping()
        self.assertEqual(ms, -1)
        self.assertIn("boom", err)

    def test_api_exception_reports_unreachable(self):
        def fake_api(*a, **k):
            raise RuntimeError("conn reset")
        ron_daemon.api = fake_api
        ms, err = ron_daemon._measure_ping()
        self.assertEqual(ms, -1)
        self.assertIn("conn reset", err)

    def test_fresh_ema_returns_average_without_api_call(self):
        # A fresh rolling average answers instantly — no getMe burned.
        calls = []
        ron_daemon.api = lambda *a, **k: calls.append(a) or {"ok": True}
        ron_daemon._note_api_latency(200.0)
        ron_daemon._note_api_latency(200.0)
        ms, err = ron_daemon._measure_ping()
        self.assertIsNone(err)
        self.assertEqual(ms, 200)
        self.assertEqual(calls, [])

    def test_stale_ema_falls_back_to_live_getme(self):
        ron_daemon.api = lambda *a, **k: {"ok": True, "result": {}}
        ron_daemon._API_LAT_EMA = 200.0
        ron_daemon._API_LAT_TS = time.time() - 9999  # older than max age
        ms, err = ron_daemon._measure_ping()
        self.assertIsNone(err)
        self.assertGreaterEqual(ms, 0)

    def test_api_feeds_ema_on_success(self):
        # api() itself records latency for successful non-poll calls.
        class FakePool:
            def post(self, *a, **k):
                time.sleep(0.02)
                return 200, b'{"ok": true}'
        self._orig_pool = ron_daemon.http_pool.pool_for
        ron_daemon.http_pool.pool_for = lambda host: FakePool()
        try:
            r = ron_daemon.api("sendMessage", {"chat_id": 1}, timeout=5)
            self.assertTrue(r["ok"])
            self.assertIsNotNone(ron_daemon._API_LAT_EMA)
            self.assertGreaterEqual(ron_daemon._API_LAT_EMA, 15)
        finally:
            ron_daemon.http_pool.pool_for = self._orig_pool

    def test_api_skips_ema_for_getupdates(self):
        # Long-poll waits are not latency — they must not feed the average.
        class FakePool:
            def post(self, *a, **k):
                time.sleep(0.02)
                return 200, b'{"ok": true, "result": []}'
        self._orig_pool = ron_daemon.http_pool.pool_for
        ron_daemon.http_pool.pool_for = lambda host: FakePool()
        try:
            ron_daemon.api("getUpdates", {"timeout": 50}, timeout=5)
            self.assertIsNone(ron_daemon._API_LAT_EMA)
        finally:
            ron_daemon.http_pool.pool_for = self._orig_pool


class PingHandlerTest(unittest.TestCase):
    def setUp(self):
        self._orig_api = ron_daemon.api
        self._orig_send = ron_daemon.send
        self._orig_ema = ron_daemon._API_LAT_EMA
        self._orig_ts = ron_daemon._API_LAT_TS
        ron_daemon._API_LAT_EMA = None
        ron_daemon._API_LAT_TS = 0.0
        self.sent = []
        ron_daemon.send = lambda chat_id, text, **k: self.sent.append(text)

    def tearDown(self):
        ron_daemon.api = self._orig_api
        ron_daemon.send = self._orig_send
        ron_daemon._API_LAT_EMA = self._orig_ema
        ron_daemon._API_LAT_TS = self._orig_ts

    def test_ping_replies_with_ms(self):
        ron_daemon.api = lambda *a, **k: {"ok": True, "result": {}}
        ok = ron_daemon.handle_command(123, 456, "ping", True)
        self.assertTrue(ok)
        self.assertEqual(len(self.sent), 1)
        self.assertRegex(self.sent[0], r"Pong . \d+ms")

    def test_ping_reports_api_failure(self):
        ron_daemon.api = lambda *a, **k: {"ok": False, "error": "HTTP 502"}
        ok = ron_daemon.handle_command(123, 456, "ping", True)
        self.assertTrue(ok)
        self.assertIn("unreachable", self.sent[0])


if __name__ == "__main__":
    unittest.main()
