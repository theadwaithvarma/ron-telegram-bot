#!/usr/bin/env python3
"""Owner-only /active command tests: activity ledger, window filtering,
in-flight "Now" tracking, owner gating, and the empty case.

No network, no Telegram. Run: python3 -m unittest discover tests -v
(from ~/workspace/ron-telegram-bot)
"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_daemon

OWNER = ron_daemon.OWNER_ID
OTHER = 999888777  # a random non-owner user


def _msg(user_id, text="/active", first_name="U", username=None,
         chat_id=12345, chat_type="private", chat_title=None):
    frm = {"id": user_id, "first_name": first_name}
    if username:
        frm["username"] = username
    chat = {"id": chat_id, "type": chat_type}
    if chat_title:
        chat["title"] = chat_title
    return {"update_id": 1,
            "message": {"message_id": 7, "chat": chat, "from": frm,
                        "text": text}}


class ActiveTestBase(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.orig_send = ron_daemon.send
        ron_daemon.send = lambda chat_id, text, **k: self.sent.append(
            (chat_id, text)) or None
        self.orig_api = ron_daemon.api
        ron_daemon.api = lambda *a, **k: {"ok": True}
        self.orig_ai = ron_daemon.ai_reply
        ron_daemon.ai_reply = lambda *a, **k: None
        with ron_daemon._ACTIVITY_LOCK:
            ron_daemon._ACTIVITY.clear()
        with ron_daemon._INFLIGHT_LOCK:
            ron_daemon._INFLIGHT_USERS.clear()

    def tearDown(self):
        ron_daemon.send = self.orig_send
        ron_daemon.api = self.orig_api
        ron_daemon.ai_reply = self.orig_ai
        with ron_daemon._ACTIVITY_LOCK:
            ron_daemon._ACTIVITY.clear()
        with ron_daemon._INFLIGHT_LOCK:
            ron_daemon._INFLIGHT_USERS.clear()

    def active_text(self):
        self.assertEqual(len(self.sent), 1)
        return self.sent[0][1]


class OwnerGatingTest(ActiveTestBase):
    def test_nonowner_refused(self):
        ok = ron_daemon.handle_command(1, OTHER, "active", True, "/active")
        self.assertTrue(ok)
        self.assertIn("Only Adwaith", self.active_text())

    def test_owner_gets_listing_not_refusal(self):
        ok = ron_daemon.handle_command(1, OWNER, "active", True, "/active")
        self.assertTrue(ok)
        self.assertNotIn("Only Adwaith", self.active_text())


class EmptyTest(ActiveTestBase):
    def test_nobody_active(self):
        ron_daemon.handle_command(1, OWNER, "active", True, "/active")
        self.assertEqual(self.active_text(),
                         "No active users in the last 10 min.")


class RecordingTest(ActiveTestBase):
    def test_record_activity_stores_fields(self):
        ron_daemon._record_activity(42, "Kaviya", "kaviya", "private", None)
        e = ron_daemon._ACTIVITY[42]
        self.assertEqual(e["name"], "Kaviya")
        self.assertEqual(e["username"], "kaviya")
        self.assertEqual(e["where"], "DM")

    def test_record_activity_group_title(self):
        ron_daemon._record_activity(43, "Soul", None, "supergroup",
                                    "Muse Playground")
        e = ron_daemon._ACTIVITY[43]
        self.assertEqual(e["where"], "Muse Playground")
        self.assertEqual(e["username"], "")

    def test_record_activity_ignores_empty_user(self):
        ron_daemon._record_activity(0, "Nobody", None, "private", None)
        self.assertEqual(ron_daemon._ACTIVITY, {})

    def test_message_hook_records_sender(self):
        # Plain DM text: the ledger hook runs before the update is routed
        # to the (mocked) AI reply.
        ron_daemon.handle_update(_msg(OTHER, "hello"))
        e = ron_daemon._ACTIVITY.get(OTHER)
        self.assertIsNotNone(e)
        self.assertEqual(e["name"], "U")

    def test_callback_hook_records_tapper(self):
        ron_daemon.handle_callback({
            "id": "cb1",
            "from": {"id": OTHER, "first_name": "Tapper",
                     "username": "tapper"},
            "data": "q:hi",
            "message": {"chat": {"id": -1001, "type": "group",
                                 "title": "Playground"},
                        "message_id": 5},
        })
        e = ron_daemon._ACTIVITY.get(OTHER)
        self.assertIsNotNone(e)
        self.assertEqual(e["where"], "Playground")
        self.assertEqual(e["username"], "tapper")


class WindowTest(ActiveTestBase):
    def _seed(self, uid, ago_s, name="U", where="DM"):
        ron_daemon._record_activity(uid, name, None, "private", None)
        with ron_daemon._ACTIVITY_LOCK:
            ron_daemon._ACTIVITY[uid]["ts"] = time.time() - ago_s
            ron_daemon._ACTIVITY[uid]["where"] = where

    def test_now_and_recent_sections(self):
        self._seed(1, 20, "JustNow", "DM")       # <60s -> Now
        self._seed(2, 150, "Recent", "Playground")  # 2m30s -> Last 10 min
        self._seed(3, 700, "Old", "DM")          # 11m40s -> pruned
        ron_daemon.handle_command(1, OWNER, "active", True, "/active")
        text = self.active_text()
        self.assertIn("Now:", text)
        self.assertIn("JustNow", text)
        self.assertIn("Last 10 min:", text)
        self.assertIn("Recent", text)
        self.assertIn("2m ago", text)
        self.assertNotIn("Old", text)
        # JustNow must not appear in the recent section.
        self.assertNotIn("JustNow ·", text)

    def test_stale_entries_pruned(self):
        self._seed(9, 3600, "Ancient", "DM")
        ron_daemon.handle_command(1, OWNER, "active", True, "/active")
        self.assertEqual(self.active_text(),
                         "No active users in the last 10 min.")
        with ron_daemon._ACTIVITY_LOCK:
            self.assertNotIn(9, ron_daemon._ACTIVITY)

    def test_recent_sorted_newest_first(self):
        self._seed(1, 500, "Older", "DM")
        self._seed(2, 120, "Newer", "DM")
        ron_daemon.handle_command(1, OWNER, "active", True, "/active")
        text = self.active_text()
        self.assertLess(text.index("Newer"), text.index("Older"))

    def test_boundary_60s(self):
        self._seed(1, 59, "Edge", "DM")
        ron_daemon.handle_command(1, OWNER, "active", True, "/active")
        text = self.active_text()
        self.assertIn("Now:", text)
        self.assertIn("Edge", text)


class InflightTest(ActiveTestBase):
    def test_inflight_user_shown_as_replying(self):
        ron_daemon._record_activity(55, "Busy", "busybee", "supergroup",
                                    "Playground")
        with ron_daemon._ACTIVITY_LOCK:
            ron_daemon._ACTIVITY[55]["ts"] = time.time() - 300  # 5m ago
        t = threading.Thread(target=lambda: None)
        with ron_daemon._INFLIGHT_LOCK:
            ron_daemon._INFLIGHT_USERS[t] = 55
        try:
            ron_daemon.handle_command(1, OWNER, "active", True, "/active")
        finally:
            with ron_daemon._INFLIGHT_LOCK:
                ron_daemon._INFLIGHT_USERS.pop(t, None)
        text = self.active_text()
        self.assertIn("Now:", text)
        self.assertIn("Busy (@busybee)", text)
        self.assertIn("replying", text)
        # In-flight user must not be duplicated in the recent section.
        self.assertEqual(text.count("Busy"), 1)

    def test_untrack_clears_inflight_user(self):
        t = threading.Thread(target=lambda: None)
        ron_daemon._track(t, 77)
        with ron_daemon._INFLIGHT_LOCK:
            self.assertEqual(ron_daemon._INFLIGHT_USERS.get(t), 77)
        ron_daemon._untrack(t)
        with ron_daemon._INFLIGHT_LOCK:
            self.assertNotIn(t, ron_daemon._INFLIGHT_USERS)
            self.assertNotIn(t, ron_daemon._INFLIGHT)

    def test_track_without_user_untouched(self):
        t = threading.Thread(target=lambda: None)
        ron_daemon._track(t)
        with ron_daemon._INFLIGHT_LOCK:
            self.assertIn(t, ron_daemon._INFLIGHT)
            self.assertNotIn(t, ron_daemon._INFLIGHT_USERS)
        ron_daemon._untrack(t)


class RelativeTimeTest(unittest.TestCase):
    def test_just_now(self):
        now = time.time()
        self.assertEqual(ron_daemon._relative_time(now - 5, now), "just now")

    def test_minutes(self):
        now = time.time()
        self.assertEqual(ron_daemon._relative_time(now - 150, now), "2m ago")
        self.assertEqual(ron_daemon._relative_time(now - 599, now), "9m ago")


if __name__ == "__main__":
    unittest.main()
