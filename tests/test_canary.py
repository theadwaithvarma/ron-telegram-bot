#!/usr/bin/env python3
"""Canary self-test tests (v1.25.0).

_cannary_probe uses ron_ai.quick_answer (no history/memory/cache) and is
silent on success. Two consecutive failures DM the owner once; recovery
DMs once. Failures feed _record_brain_failure.

No network, no Telegram. Run: python3 tests/test_canary.py
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_daemon


class CanaryProbeTest(unittest.TestCase):
    def setUp(self):
        ron_daemon._canary_fails = 0
        ron_daemon._canary_alerted = False
        self.sent = []
        p = mock.patch.object(ron_daemon, "send",
                              lambda c, t, *a, **k: self.sent.append((c, t)))
        p.start()
        self.addCleanup(p.stop)
        # Never let the probe touch the real degraded state machine.
        b = mock.patch.object(ron_daemon, "_record_brain_failure",
                              lambda *a, **k: None)
        b.start()
        self.addCleanup(b.stop)

    def tearDown(self):
        ron_daemon._canary_fails = 0
        ron_daemon._canary_alerted = False

    def _probe(self, answer="ok", exc=None):
        def fake_qa(prompt, name=""):
            if exc is not None:
                raise exc
            return answer
        with mock.patch.object(ron_daemon.ron_ai, "quick_answer", fake_qa):
            return ron_daemon._canary_probe()

    def test_success_is_silent(self):
        self.assertTrue(self._probe("ok"))
        self.assertEqual(self.sent, [])
        self.assertEqual(ron_daemon._canary_fails, 0)

    def test_wrong_answer_counts_as_failure(self):
        self.assertFalse(self._probe("something else"))
        self.assertEqual(ron_daemon._canary_fails, 1)

    def test_exception_counts_as_failure(self):
        self.assertFalse(self._probe(exc=RuntimeError("boom")))
        self.assertEqual(ron_daemon._canary_fails, 1)

    def test_two_failures_alert_owner_once(self):
        self._probe(exc=RuntimeError("boom"))
        self.assertEqual(self.sent, [])  # first failure: silent
        self._probe(exc=RuntimeError("boom"))
        self.assertEqual(len(self.sent), 1)
        chat_id, text = self.sent[0]
        self.assertEqual(chat_id, ron_daemon.OWNER_ID)
        self.assertIn("canary", text.lower())
        # Third failure: no second DM.
        self._probe(exc=RuntimeError("boom"))
        self.assertEqual(len(self.sent), 1)

    def test_recovery_dm_once_then_silent(self):
        self._probe(exc=RuntimeError("boom"))
        self._probe(exc=RuntimeError("boom"))
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self._probe("ok"))
        self.assertEqual(len(self.sent), 2)
        self.assertIn("green", self.sent[1][1].lower())
        # Steady state after recovery: silent again.
        self.assertTrue(self._probe("ok"))
        self.assertEqual(len(self.sent), 2)

    def test_failure_feeds_brain_counter(self):
        calls = []
        with mock.patch.object(ron_daemon, "_record_brain_failure",
                               lambda e: calls.append(e)):
            self._probe(exc=RuntimeError("boom"))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
