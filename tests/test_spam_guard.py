#!/usr/bin/env python3
"""Spam guards for button callbacks and the degraded queue.

Covers the 2026-09-20 incident: a user hammering a quick-reply button
re-sent the label as a message per tap. Callback queries bypassed the
maintenance gate in handle_update, each tap burned an AI call, and the
degraded queue piled up duplicates that replayed as a spam flood.

Guards under test:
  1. handle_callback honors maintenance mode for non-owners.
  2. Rapid repeat taps of the same button are throttled (one per window).
  3. _queue_message dedupes identical (chat, user, text) entries.

No network, no Telegram. Run: python3 -m unittest discover tests -v
(from ~/workspace/telegram-bot)
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_daemon

OWNER = ron_daemon.OWNER_ID
OTHER = 999888777  # a random non-owner user


def _query(user_id, data, chat_id=-1001):
    return {
        "id": "cb1",
        "from": {"id": user_id, "first_name": "U"},
        "data": data,
        "message": {"chat": {"id": chat_id, "type": "group"},
                    "message_id": 5},
    }


class CallbackTestBase(unittest.TestCase):
    def setUp(self):
        self.orig_api = ron_daemon.api
        ron_daemon.api = lambda *a, **k: {"ok": True}
        self.ai_calls = []
        self.orig_ai = ron_daemon.ai_reply
        ron_daemon.ai_reply = lambda *a, **k: self.ai_calls.append((a, k))
        ron_daemon.set_maintenance(False, OWNER)
        ron_daemon._tap_ts.clear()

    def tearDown(self):
        ron_daemon.api = self.orig_api
        ron_daemon.ai_reply = self.orig_ai
        ron_daemon.set_maintenance(False, OWNER)
        ron_daemon._tap_ts.clear()


class CallbackMaintenanceTest(CallbackTestBase):
    def test_nonowner_button_ignored_in_maintenance(self):
        ron_daemon.set_maintenance(True, OWNER)
        ron_daemon.handle_callback(_query(OTHER, "q:hello"))
        self.assertEqual(self.ai_calls, [])

    def test_owner_button_works_in_maintenance(self):
        ron_daemon.set_maintenance(True, OWNER)
        ron_daemon.handle_callback(_query(OWNER, "q:hello"))
        self.assertEqual(len(self.ai_calls), 1)

    def test_nonowner_button_works_outside_maintenance(self):
        ron_daemon.handle_callback(_query(OTHER, "q:hello"))
        self.assertEqual(len(self.ai_calls), 1)


class TapThrottleTest(CallbackTestBase):
    def test_rapid_repeat_taps_dropped(self):
        q = _query(OTHER, "q:hello")
        for _ in range(3):
            ron_daemon.handle_callback(q)
        self.assertEqual(len(self.ai_calls), 1)

    def test_different_buttons_not_throttled(self):
        ron_daemon.handle_callback(_query(OTHER, "q:aaa"))
        ron_daemon.handle_callback(_query(OTHER, "q:bbb"))
        self.assertEqual(len(self.ai_calls), 2)

    def test_different_users_not_throttled(self):
        ron_daemon.handle_callback(_query(OTHER, "q:hello"))
        ron_daemon.handle_callback(_query(OTHER + 1, "q:hello"))
        self.assertEqual(len(self.ai_calls), 2)

    def test_tap_after_window_fires_again(self):
        q = _query(OTHER, "q:hello")
        ron_daemon.handle_callback(q)
        key = (-1001, OTHER, "q:hello")
        ron_daemon._tap_ts[key] -= ron_daemon._TAP_WINDOW + 1
        ron_daemon.handle_callback(q)
        self.assertEqual(len(self.ai_calls), 2)


class QueueDedupeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.orig_q = ron_daemon.DEGRADED_QUEUE
        ron_daemon.DEGRADED_QUEUE = self.tmp.name

    def tearDown(self):
        ron_daemon.DEGRADED_QUEUE = self.orig_q
        os.unlink(self.tmp.name)

    def _lines(self):
        with open(self.tmp.name) as f:
            return [ln for ln in f if ln.strip()]

    def test_identical_message_queued_once(self):
        ron_daemon._queue_message(-1001, OTHER, "U", "hello")
        ron_daemon._queue_message(-1001, OTHER, "U", "hello")
        self.assertEqual(len(self._lines()), 1)

    def test_different_text_queued_twice(self):
        ron_daemon._queue_message(-1001, OTHER, "U", "hello")
        ron_daemon._queue_message(-1001, OTHER, "U", "world")
        self.assertEqual(len(self._lines()), 2)

    def test_different_user_queued_twice(self):
        ron_daemon._queue_message(-1001, OTHER, "U", "hello")
        ron_daemon._queue_message(-1001, OTHER + 1, "V", "hello")
        self.assertEqual(len(self._lines()), 2)


if __name__ == "__main__":
    unittest.main()
