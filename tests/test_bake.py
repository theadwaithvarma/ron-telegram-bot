#!/usr/bin/env python3
"""Canary deploy (bake window) tests (v1.25.0).

For 10 min after (re)start only the owner is served; everyone else is
queued with a "warming up" notice (never the degraded text). At bake end
the queue drains once. Owner messages pass through normally.

No network, no Telegram. Run: python3 tests/test_bake.py
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_daemon

OWNER_ID = ron_daemon.OWNER_ID


class BakeWindowTest(unittest.TestCase):
    def setUp(self):
        self.orig_bake = ron_daemon._BAKE_UNTIL
        ron_daemon._bake_noticed.clear()

    def tearDown(self):
        ron_daemon._BAKE_UNTIL = self.orig_bake
        ron_daemon._bake_noticed.clear()

    def _run_ai_reply(self, user_id, bake_active=True, **kw):
        """Drive ai_reply; capture routing decisions."""
        events = {"queued": [], "notices": [], "chats": []}
        ron_daemon._BAKE_UNTIL = (time.time() + 600 if bake_active
                                  else time.time() - 1)

        class DummyKick:
            def join(self, timeout=None):
                pass

        patches = [
            mock.patch.object(ron_daemon, "_brain_down", lambda: False),
            mock.patch.object(
                ron_daemon, "_queue_message",
                lambda c, u, n, t, *a, **k: events["queued"].append(u)),
            mock.patch.object(
                ron_daemon, "_maybe_bake_notice",
                lambda c, *a, **k: events["notices"].append(c)),
            mock.patch.object(
                ron_daemon.ron_ai, "chat",
                lambda *a, **k: events["chats"].append(a) or "served"),
            mock.patch.object(ron_daemon.ron_ai, "light_mode_enabled",
                              lambda: False),
            mock.patch.object(ron_daemon, "group_context", lambda: ""),
            mock.patch.object(ron_daemon, "typing_kickoff",
                              lambda *a, **k: DummyKick()),
            mock.patch.object(ron_daemon, "send_rich_with_kb",
                              lambda *a, **k: None),
            mock.patch.object(ron_daemon, "handle_group_sends",
                              lambda *a, **k: None),
            mock.patch.object(ron_daemon, "handle_send_topic",
                              lambda *a, **k: None),
            mock.patch.object(ron_daemon, "handle_topic_actions",
                              lambda *a, **k: None),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        ron_daemon.ai_reply(111, user_id, "Someone", "hello",
                            private=False, cacheable=False, **kw)
        for _ in range(100):
            if events["queued"] or events["chats"]:
                break
            time.sleep(0.05)
        return events

    def test_stranger_queued_with_notice_during_bake(self):
        events = self._run_ai_reply(999999)
        self.assertEqual(events["queued"], [999999])
        self.assertEqual(events["notices"], [111])
        self.assertEqual(events["chats"], [])  # AI never ran

    def test_owner_served_during_bake(self):
        events = self._run_ai_reply(OWNER_ID)
        self.assertEqual(events["queued"], [])
        self.assertEqual(events["notices"], [])
        self.assertEqual(len(events["chats"]), 1)

    def test_bake_expired_serves_everyone(self):
        events = self._run_ai_reply(999999, bake_active=False)
        self.assertEqual(events["queued"], [])
        self.assertEqual(events["notices"], [])
        self.assertEqual(len(events["chats"]), 1)

    def test_bake_notice_text_is_not_degraded_text(self):
        notices = []
        with mock.patch.object(ron_daemon, "send_rich_with_kb",
                               lambda c, t, *a, **k: notices.append(t)):
            ron_daemon._maybe_bake_notice(111)
        self.assertEqual(len(notices), 1)
        self.assertIn("warming up", notices[0])
        self.assertNotIn("every AI backend", notices[0])

    def test_bake_notice_once_per_chat(self):
        notices = []
        with mock.patch.object(ron_daemon, "send_rich_with_kb",
                               lambda c, t, *a, **k: notices.append(c)):
            ron_daemon._maybe_bake_notice(111)
            ron_daemon._maybe_bake_notice(111)
            ron_daemon._maybe_bake_notice(222)
        self.assertEqual(notices, [111, 222])

    def test_bake_end_drains_queue(self):
        drained = []
        ron_daemon._BAKE_UNTIL = time.time() - 5  # already over
        with mock.patch.object(ron_daemon, "_drain_queue",
                               lambda: drained.append(True) or True):
            ron_daemon._bake_drain_loop()
        self.assertEqual(drained, [True])


if __name__ == "__main__":
    unittest.main()
