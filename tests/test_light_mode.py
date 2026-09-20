#!/usr/bin/env python3
"""Offline tests for light mode (no network, no Telegram).

Run: python3 tests/test_light_mode.py  (or python3 -m unittest discover tests)

Covers: default flag off, cheap-routing forcing, and the once-per-user-per-day
light-mode notice (including corrupt ledger and missing user id).
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_ai

HEAVY = ("Write a detailed essay on the history of operating systems, "
         "from UNIX to Linux, with key design decisions explained")


class LightFlagTest(unittest.TestCase):
    def test_default_off(self):
        self.assertFalse(ron_ai.DEFAULT_CONFIG["light_mode"])

    def test_enabled_reads_config(self):
        self.assertTrue(ron_ai.light_mode_enabled({"light_mode": True}))
        self.assertFalse(ron_ai.light_mode_enabled({"light_mode": False}))
        self.assertFalse(ron_ai.light_mode_enabled({}))


class CheapRoutingTest(unittest.TestCase):
    def test_trivial_unaffected(self):
        # Small-talk is cheap with or without light mode.
        self.assertTrue(ron_ai._use_cheap("hi", {"light_mode": False}))
        self.assertTrue(ron_ai._use_cheap("hi", {"light_mode": True}))

    def test_forced_in_light_mode(self):
        self.assertFalse(ron_ai._use_cheap(HEAVY, {"light_mode": False}))
        self.assertTrue(ron_ai._use_cheap(HEAVY, {"light_mode": True}))
        self.assertFalse(ron_ai._use_cheap(HEAVY, {}))


class NoticeTest(unittest.TestCase):
    def _tmp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        os.unlink(tmp.name)  # notice helper must create it
        self.addCleanup(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name))
        return tmp.name

    def test_once_per_day(self):
        p = self._tmp()
        first = ron_ai.light_mode_notice(123, state_file=p)
        self.assertIn("Light mode", first)
        # Same user, same day -> silence.
        self.assertEqual(ron_ai.light_mode_notice(123, state_file=p), "")
        # Different user still gets it.
        self.assertIn("Light mode",
                      ron_ai.light_mode_notice(456, state_file=p))
        seen = json.load(open(p))
        self.assertEqual(set(seen), {"123", "456"})

    def test_again_after_day_change(self):
        p = self._tmp()
        with open(p, "w") as f:
            json.dump({"123": "2000-01-01"}, f)
        self.assertIn("Light mode",
                      ron_ai.light_mode_notice(123, state_file=p))

    def test_corrupt_ledger_never_breaks_reply(self):
        p = self._tmp()
        with open(p, "w") as f:
            f.write("not json{{{")
        self.assertIn("Light mode",
                      ron_ai.light_mode_notice(7, state_file=p))

    def test_no_user_id_no_notice_no_file(self):
        p = self._tmp()
        self.assertEqual(ron_ai.light_mode_notice(0, state_file=p), "")
        self.assertEqual(ron_ai.light_mode_notice(None, state_file=p), "")
        self.assertFalse(os.path.exists(p))


if __name__ == "__main__":
    unittest.main(verbosity=2)
