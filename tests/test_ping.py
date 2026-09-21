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

    def tearDown(self):
        ron_daemon.api = self._orig_api

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


class PingHandlerTest(unittest.TestCase):
    def setUp(self):
        self._orig_api = ron_daemon.api
        self._orig_send = ron_daemon.send
        self.sent = []
        ron_daemon.send = lambda chat_id, text, **k: self.sent.append(text)

    def tearDown(self):
        ron_daemon.api = self._orig_api
        ron_daemon.send = self._orig_send

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
