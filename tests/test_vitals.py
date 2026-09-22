#!/usr/bin/env python3
"""Offline tests for the v1.31.0 vitals tracker (no network).

ron_ai gains a tiny thread-safe request tracker: every provider attempt
(ok / http_NNN / exception) and every completed streaming turn
(first-token ms, total ms) is recorded into a rolling per-provider window
and persisted (throttled) to ron_stats.json. get_vitals() exposes
per-provider turn counts, ttfb p50/p95, and error counts for the owner's
/vitals command. Verifies:
  - turn recording + percentile math (p50/p95)
  - attempt recording incl. error kinds and counts
  - persistence round-trip through a temp STATS_FILE
  - record_* never raise, even with garbage input
  - get_vitals is read-only and never raises
"""
import importlib.util as _ilu
import json
import os
import sys
import tempfile
import threading
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)
for _m in ("ron_ai", "ron_ai_under_test"):
    sys.modules.pop(_m, None)

_spec = _ilu.spec_from_file_location(
    "ron_ai_vitals_test",
    os.path.join(REPO, "ron_ai.py"))
ron_ai = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(ron_ai)
assert "ron-telegram-bot" in ron_ai.__file__, ron_ai.__file__


class VitalsTest(unittest.TestCase):
    def setUp(self):
        # Isolate: fresh deques + temp stats file per test.
        self._tmp = tempfile.TemporaryDirectory()
        self._old_file = ron_ai.STATS_FILE
        self._old_flush = ron_ai._STATS_FLUSH_SECS
        ron_ai.STATS_FILE = os.path.join(self._tmp.name, "ron_stats.json")
        ron_ai._STATS_FLUSH_SECS = 0  # flush on every record in tests
        with ron_ai._stats_lock:
            ron_ai._stats_turns.clear()
            ron_ai._stats_attempts.clear()
            ron_ai._stats_last_flush = 0.0

    def tearDown(self):
        ron_ai.STATS_FILE = self._old_file
        ron_ai._STATS_FLUSH_SECS = self._old_flush
        with ron_ai._stats_lock:
            ron_ai._stats_turns.clear()
            ron_ai._stats_attempts.clear()
        self._tmp.cleanup()

    def test_turn_percentiles(self):
        for ms in (100, 200, 300, 400):
            ron_ai.record_turn("groq", "m", ms, ms + 50)
        v = ron_ai.get_vitals()["groq"]
        self.assertEqual(v["turns"], 4)
        self.assertAlmostEqual(v["ttfb_p50_ms"], 250.0)
        self.assertAlmostEqual(v["ttfb_p95_ms"], 385.0, places=0)
        self.assertAlmostEqual(v["total_p50_ms"], 300.0)

    def test_attempt_errors_counted(self):
        ron_ai.record_attempt("groq", "m", True, 500, "ok")
        ron_ai.record_attempt("groq", "m", False, 120, "http_429")
        ron_ai.record_attempt("groq", "m", False, 90, "http_500")
        v = ron_ai.get_vitals()["groq"]
        self.assertEqual(v["attempts"], 3)
        self.assertEqual(v["errors"], 2)
        self.assertEqual(v["last_error"], "http_500")

    def test_unknown_provider_absent(self):
        self.assertNotIn("gemini", ron_ai.get_vitals())

    def test_persistence_round_trip(self):
        ron_ai.record_turn("pollinations", "openai", 4300, 9000)
        ron_ai.record_attempt("pollinations", "openai", False, 10,
                              "exception")
        with ron_ai._stats_lock:
            ron_ai._stats_turns.clear()
            ron_ai._stats_attempts.clear()
        ron_ai._stats_load()
        v = ron_ai.get_vitals()["pollinations"]
        self.assertEqual(v["turns"], 1)
        self.assertAlmostEqual(v["ttfb_p50_ms"], 4300.0)
        self.assertEqual(v["errors"], 1)
        # File is real JSON on disk.
        data = json.load(open(ron_ai.STATS_FILE))
        self.assertIn("pollinations", data["turns"])

    def test_rolling_window_bounded(self):
        for i in range(ron_ai._STATS_WINDOW + 50):
            ron_ai.record_turn("groq", "m", 100 + i, 200 + i)
        v = ron_ai.get_vitals()["groq"]
        self.assertEqual(v["turns"], ron_ai._STATS_WINDOW)

    def test_record_never_raises(self):
        ron_ai.record_turn(None, None, "bad", float("nan"))
        ron_ai.record_attempt(None, None, "x", None, None)
        ron_ai.get_vitals()  # must not raise either

    def test_thread_safety_smoke(self):
        def work(n):
            for i in range(50):
                ron_ai.record_turn("groq", "m", 100 + i, 200)
                ron_ai.record_attempt("groq", "m", i % 2 == 0, 100, "ok")

        threads = [threading.Thread(target=work, args=(t,))
                   for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        v = ron_ai.get_vitals()["groq"]
        self.assertEqual(v["turns"], 200)
        self.assertEqual(v["attempts"], 200)

    def test_percentile_edges(self):
        self.assertEqual(ron_ai._percentile([], 50), 0.0)
        self.assertEqual(ron_ai._percentile([42], 95), 42.0)


if __name__ == "__main__":
    unittest.main()
