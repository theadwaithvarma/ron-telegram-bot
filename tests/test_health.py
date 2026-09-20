#!/usr/bin/env python3
"""Phase 7 tests: /health status card. No network, no Telegram.

Run: python3 -m unittest discover tests -v   (from ~/workspace/telegram-bot)
"""
import os
import re
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_health
import ron_daemon


def full_info(**over):
    info = {
        "start_ts": time.time() - 90061,  # 1d 1h 1m 1s ago
        "degraded": False,
        "providers": [("primary", "ok", None),
                      ("fallback-1", "ok", None),
                      ("fallback-2", "cooling", 180.0)],
        "memory_on": True,
        "summaries": 5,
        "cache_hits": 12,
        "cache_misses": 88,
        "cache_tokens": 1500,
        "backup_ts": time.time() - 6 * 3600,
        "restarts": 6,
    }
    info.update(over)
    return info


class CardContentTest(unittest.TestCase):
    def test_all_sections_present(self):
        card = ron_health.build_health_card(full_info())
        for needle in ("Ron health", "up 1d 1h", "brain ok", "primary",
                       "fallback-1", "fallback-2", "memory on",
                       "5 chats summarized", "cache", "12% hit",
                       "tokens saved", "backup", "6h ago", "restarts: 6"):
            self.assertIn(needle, card)

    def test_degraded_brain_flagged(self):
        card = ron_health.build_health_card(full_info(degraded=True))
        self.assertIn("degraded", card)

    def test_memory_off(self):
        card = ron_health.build_health_card(full_info(memory_on=False))
        self.assertIn("memory off", card)
        self.assertNotIn("summarized", card)

    def test_compact_enough_for_a_bubble(self):
        card = ron_health.build_health_card(full_info())
        self.assertLess(len(card), 600)
        self.assertLessEqual(len(card.splitlines()), 8)

    def test_empty_info_degrades_gracefully(self):
        card = ron_health.build_health_card({})
        self.assertIn("Ron health", card)
        self.assertIn("n/a", card)

    def test_none_info_degrades_gracefully(self):
        card = ron_health.build_health_card(None)
        self.assertIn("Ron health", card)


class PrivacyTest(unittest.TestCase):
    """The card must never name providers or leak key-like material."""

    def test_no_provider_names(self):
        card = ron_health.build_health_card(full_info())
        low = card.lower()
        for name in ("groq", "gemini", "openrouter", "gpt", "llama",
                     "nemotron"):
            self.assertNotIn(name, low)

    def test_cooling_label_still_neutral(self):
        info = full_info(providers=[("primary", "cooling", 65.0)])
        card = ron_health.build_health_card(info)
        self.assertIn("primary cooling", card)
        self.assertNotIn("groq", card.lower())

    def test_bad_label_rejected(self):
        # Even if a caller passes a real provider name as a label, the
        # card builder must refuse rather than leak it.
        with self.assertRaises(ValueError):
            ron_health.build_health_card(
                full_info(providers=[("groq", "ok", None)]))

    def test_no_long_tokens_in_card(self):
        card = ron_health.build_health_card(full_info())
        # Nothing that looks like a key/blob: no 40+ char word runs.
        long_words = [w for w in re.split(r"\s+", card) if len(w) > 40]
        self.assertEqual(long_words, [])


    def test_failing_backup_flagged(self):
        old = time.time() - 48 * 3600
        card = ron_health.build_health_card(
            full_info(backup_ts=old, backup_error="boom"))
        self.assertIn("⚠️ failing", card)

    def test_fresh_backup_not_flagged(self):
        card = ron_health.build_health_card(full_info())
        self.assertNotIn("failing", card)

    def test_no_backup_yet(self):
        card = ron_health.build_health_card(full_info(backup_ts=None))
        self.assertIn("backup none yet", card)


class FormatHelpersTest(unittest.TestCase):
    def test_uptime(self):
        self.assertEqual(ron_health._uptime(45), "45s")
        self.assertEqual(ron_health._uptime(90), "1m")
        self.assertEqual(ron_health._uptime(3700), "1h 1m")
        self.assertEqual(ron_health._uptime(90061), "1d 1h")
        self.assertEqual(ron_health._uptime(None), "n/a")

    def test_age(self):
        now = 1000000.0
        self.assertEqual(ron_health._age(now - 30, now), "30s")
        self.assertEqual(ron_health._age(now - 300, now), "5m")
        self.assertEqual(ron_health._age(now - 7200, now), "2h")
        self.assertEqual(ron_health._age(now - 172800, now), "2d")
        self.assertEqual(ron_health._age(None, now), "n/a")

    def test_tokens(self):
        self.assertEqual(ron_health._tokens(999), "999")
        self.assertEqual(ron_health._tokens(1500), "~1.5k")
        self.assertEqual(ron_health._tokens(None), "n/a")


class DaemonWiringTest(unittest.TestCase):
    """handle_command routes /health; _handle_health sends one message."""

    def setUp(self):
        self.sent = []
        self.orig_send = ron_daemon.send
        ron_daemon.send = lambda chat_id, text, **kw: self.sent.append(
            (chat_id, text))
        self.orig_mem = ron_daemon.mem
        ron_daemon.mem = None  # memory off path

    def tearDown(self):
        ron_daemon.send = self.orig_send
        ron_daemon.mem = self.orig_mem

    def test_health_command_sends_card(self):
        ok = ron_daemon.handle_command(123, 456, "health", True, "/health",
                                       "Test")
        self.assertTrue(ok)
        self.assertEqual(len(self.sent), 1)
        chat_id, text = self.sent[0]
        self.assertEqual(chat_id, 123)
        self.assertIn("Ron health", text)
        self.assertNotIn("groq", text.lower())

    def test_health_with_at_botname(self):
        self.assertEqual(ron_daemon.parse_command("/health@ronrebootedbot"),
                         "health")

    def test_health_other_bot_ignored(self):
        self.assertIsNone(ron_daemon.parse_command("/health@otherbot"))

    def test_health_never_raises(self):
        # Even with everything broken, /health answers, not crashes.
        orig = ron_daemon.ron_health
        ron_daemon.ron_health = None
        try:
            ok = ron_daemon.handle_command(123, 456, "health", True,
                                           "/health", "Test")
            self.assertTrue(ok)
            self.assertEqual(len(self.sent), 1)
        finally:
            ron_daemon.ron_health = orig

    def test_probe_results_recorded(self):
        ron_daemon._record_probe_results({"x": (True, "ok")})
        self.assertEqual(ron_daemon._LAST_PROBE["x"], (True, "ok"))

    def test_start_ts_set(self):
        self.assertLess(ron_daemon._START_TS, time.time() + 1)


if __name__ == "__main__":
    unittest.main()
