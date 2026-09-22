#!/usr/bin/env python3
"""Stale chat radar tests (v1.25.0).

Bookkeeping: incoming business messages mark a chat waiting; an observed
outgoing (owner-human) message clears it. Digest: once/day ~09:00 IST,
chats waiting >24h with no reply since; silent when empty.

No network, no Telegram. Run: python3 tests/test_stale_radar.py
"""
import datetime
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_daemon

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
# 2026-09-22 09:30 IST
TUE_0930_IST = datetime.datetime(2026, 9, 22, 9, 30, tzinfo=IST).timestamp()
TUE_0800_IST = datetime.datetime(2026, 9, 22, 8, 0, tzinfo=IST).timestamp()

CONN_ID = "test_conn_stale"
OWNER = 6819786239


class StaleRadarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = ron_daemon.BUSINESS_STALE_FILE
        ron_daemon.BUSINESS_STALE_FILE = os.path.join(self.tmp.name,
                                                      "business_stale.json")
        self.sent = []
        p = mock.patch.object(ron_daemon, "send",
                              lambda c, t, *a, **k: self.sent.append((c, t)))
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        ron_daemon.BUSINESS_STALE_FILE = self.orig
        self.tmp.cleanup()

    def test_incoming_tracked(self):
        ron_daemon._stale_note_incoming("555", "Cust", "hello there",
                                        now=1000.0)
        store = ron_daemon._stale_load()
        e = store["chats"]["555"]
        self.assertEqual(e["name"], "Cust")
        self.assertEqual(e["snippet"], "hello there")
        self.assertEqual(e["last_in_ts"], 1000.0)

    def test_snippet_truncated_to_80(self):
        ron_daemon._stale_note_incoming("555", "Cust", "x" * 200, now=1.0)
        e = ron_daemon._stale_load()["chats"]["555"]
        self.assertEqual(len(e["snippet"]), 80)

    def test_outgoing_clears_staleness(self):
        ron_daemon._stale_note_incoming("555", "Cust", "hi", now=1000.0)
        ron_daemon._stale_note_outgoing("555", now=1100.0)
        items = ron_daemon._stale_digest_list(now=1000.0 + 30 * 3600)
        self.assertEqual(items, [])

    def test_digest_lists_only_old_unreplied(self):
        now = 10_000_000.0
        # waiting 30h, no reply -> listed
        ron_daemon._stale_note_incoming("1", "Old", "waiting...", now=now - 30 * 3600)
        # waiting 30h but replied after -> not listed
        ron_daemon._stale_note_incoming("2", "Replied", "thanks!", now=now - 30 * 3600)
        ron_daemon._stale_note_outgoing("2", now=now - 29 * 3600)
        # only 5h old -> not listed
        ron_daemon._stale_note_incoming("3", "Fresh", "hey", now=now - 5 * 3600)
        items = ron_daemon._stale_digest_list(now=now)
        self.assertEqual([c for c, _, _, _ in items], ["1"])
        cid, name, hours, snippet = items[0]
        self.assertEqual(name, "Old")
        self.assertAlmostEqual(hours, 30.0, places=1)
        self.assertEqual(snippet, "waiting...")

    def test_digest_sorted_oldest_first(self):
        now = 10_000_000.0
        ron_daemon._stale_note_incoming("1", "Newer", "a", now=now - 25 * 3600)
        ron_daemon._stale_note_incoming("2", "Older", "b", now=now - 50 * 3600)
        items = ron_daemon._stale_digest_list(now=now)
        self.assertEqual([c for c, _, _, _ in items], ["2", "1"])

    def test_format_digest(self):
        text = ron_daemon._stale_format_digest(
            [("1", "Cust", 31.4, "do you deliver?")])
        self.assertIn("Cust", text)
        self.assertIn("31h", text)
        self.assertIn("do you deliver?", text)

    def test_maybe_digest_sends_at_9am(self):
        ron_daemon._stale_note_incoming("1", "Cust", "hello?",
                                        now=TUE_0930_IST - 30 * 3600)
        self.assertTrue(ron_daemon._stale_maybe_digest(now=TUE_0930_IST))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], ron_daemon.OWNER_ID)
        self.assertIn("Cust", self.sent[0][1])
        # Second call same day: silent.
        self.assertFalse(ron_daemon._stale_maybe_digest(now=TUE_0930_IST + 60))
        self.assertEqual(len(self.sent), 1)

    def test_maybe_digest_failed_send_retries_next_pass(self):
        # A failed send must NOT mark the day digested — the digest
        # retries on the next hourly pass instead of skipping the day.
        ron_daemon._stale_note_incoming("1", "Cust", "hello?",
                                        now=TUE_0930_IST - 30 * 3600)
        with mock.patch.object(ron_daemon, "send",
                               side_effect=RuntimeError("net down")):
            self.assertFalse(ron_daemon._stale_maybe_digest(now=TUE_0930_IST))
        store = ron_daemon._stale_load()
        self.assertNotEqual(store.get("last_digest_day"), "2026-09-22")
        # Next pass, send works: digest goes out, day marked.
        self.assertTrue(ron_daemon._stale_maybe_digest(now=TUE_0930_IST + 60))
        self.assertEqual(len(self.sent), 1)
        store = ron_daemon._stale_load()
        self.assertEqual(store.get("last_digest_day"), "2026-09-22")

    def test_maybe_digest_silent_when_empty(self):
        self.assertFalse(ron_daemon._stale_maybe_digest(now=TUE_0930_IST))
        self.assertEqual(self.sent, [])
        # Day still marked so we don't recompute hourly.
        store = ron_daemon._stale_load()
        self.assertEqual(store.get("last_digest_day"), "2026-09-22")

    def test_maybe_digest_not_before_9am(self):
        ron_daemon._stale_note_incoming("1", "Cust", "hello?",
                                        now=TUE_0800_IST - 30 * 3600)
        self.assertFalse(ron_daemon._stale_maybe_digest(now=TUE_0800_IST))
        self.assertEqual(self.sent, [])

    def test_business_message_hooks(self):
        """Incoming stranger tracked; owner outgoing clears; bot echo ignored."""
        conns = {CONN_ID: {"user_id": OWNER, "can_reply": True}}
        orig_conns = ron_daemon.BUSINESS_CONNECTIONS_FILE
        ctmp = tempfile.TemporaryDirectory()
        self.addCleanup(ctmp.cleanup)
        ron_daemon.BUSINESS_CONNECTIONS_FILE = os.path.join(ctmp.name, "bc.json")
        with open(ron_daemon.BUSINESS_CONNECTIONS_FILE, "w") as f:
            json.dump(conns, f)
        self.addCleanup(lambda: setattr(ron_daemon, "BUSINESS_CONNECTIONS_FILE",
                                        orig_conns))
        orig_ai = ron_daemon.ai_reply
        ron_daemon.ai_reply = lambda *a, **k: None
        self.addCleanup(lambda: setattr(ron_daemon, "ai_reply", orig_ai))
        orig_banned = ron_daemon.ron_owner.is_banned
        ron_daemon.ron_owner.is_banned = lambda u: False
        self.addCleanup(lambda: setattr(ron_daemon.ron_owner, "is_banned",
                                        orig_banned))
        orig_maint = ron_daemon.maintenance_on
        ron_daemon.maintenance_on = lambda: False
        self.addCleanup(lambda: setattr(ron_daemon, "maintenance_on",
                                        orig_maint))

        def msg(sender, is_bot=False, text="hey adwaith"):
            return {"business_connection_id": CONN_ID,
                    "chat": {"id": 777, "type": "private"},
                    "message_id": 1, "text": text,
                    "from": {"id": sender, "first_name": "Cust",
                             "is_bot": is_bot}}

        ron_daemon.handle_business_message(msg(111222333))
        e = ron_daemon._stale_load()["chats"]["777"]
        self.assertEqual(e["name"], "Cust")
        # Bot echo: neither incoming nor outgoing.
        ron_daemon.handle_business_message(msg(999, is_bot=True))
        self.assertNotIn("replied_ts", ron_daemon._stale_load()["chats"]["777"])
        # Owner's own message: clears staleness.
        ron_daemon.handle_business_message(msg(OWNER, text="on my way"))
        e = ron_daemon._stale_load()["chats"]["777"]
        self.assertIn("replied_ts", e)


if __name__ == "__main__":
    unittest.main()
