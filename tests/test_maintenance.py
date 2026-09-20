#!/usr/bin/env python3
"""Maintenance mode tests: owner-only /shutdown and /start (resume).

No network, no Telegram. Run: python3 -m unittest discover tests -v
(from ~/workspace/telegram-bot)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_daemon

OWNER = ron_daemon.OWNER_ID
OTHER = 999888777  # a random non-owner user


class MaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.orig_send = ron_daemon.send
        ron_daemon.send = lambda chat_id, text, **kw: self.sent.append(
            (chat_id, text))
        self.ai_calls = []
        self.orig_ai = ron_daemon.ai_reply
        ron_daemon.ai_reply = lambda *a, **k: self.ai_calls.append((a, k))
        # Start each test with maintenance off.
        ron_daemon.set_maintenance(False, OWNER)

    def tearDown(self):
        ron_daemon.send = self.orig_send
        ron_daemon.ai_reply = self.orig_ai
        ron_daemon.set_maintenance(False, OWNER)

    # --- flag persistence ------------------------------------------------
    def test_flag_roundtrip(self):
        self.assertFalse(ron_daemon.maintenance_on())
        ron_daemon.set_maintenance(True, OWNER)
        self.assertTrue(ron_daemon.maintenance_on())
        ron_daemon.set_maintenance(False, OWNER)
        self.assertFalse(ron_daemon.maintenance_on())

    def test_missing_file_means_off(self):
        if os.path.exists(ron_daemon.MAINT_FILE):
            os.remove(ron_daemon.MAINT_FILE)
        self.assertFalse(ron_daemon.maintenance_on())

    # --- /shutdown -------------------------------------------------------
    def test_owner_shutdown_enables(self):
        ok = ron_daemon.handle_command(1, OWNER, "shutdown", True,
                                       "/shutdown", "Adwaith")
        self.assertTrue(ok)
        self.assertTrue(ron_daemon.maintenance_on())
        self.assertTrue(any("Maintenance mode on" in t for _, t in self.sent))

    def test_nonowner_shutdown_refused(self):
        ok = ron_daemon.handle_command(1, OTHER, "shutdown", True,
                                       "/shutdown", "Bob")
        self.assertTrue(ok)
        self.assertFalse(ron_daemon.maintenance_on())
        self.assertTrue(any("Only Adwaith" in t for _, t in self.sent))

    # --- /start ----------------------------------------------------------
    def test_owner_start_wakes_up(self):
        ron_daemon.set_maintenance(True, OWNER)
        ok = ron_daemon.handle_command(1, OWNER, "start", True,
                                       "/start", "Adwaith")
        self.assertTrue(ok)
        self.assertFalse(ron_daemon.maintenance_on())
        self.assertTrue(any("Back online" in t for _, t in self.sent))

    def test_nonowner_start_gets_break_notice(self):
        ron_daemon.set_maintenance(True, OWNER)
        ok = ron_daemon.handle_command(1, OTHER, "start", True,
                                       "/start", "Bob")
        self.assertTrue(ok)
        self.assertTrue(ron_daemon.maintenance_on())  # still down
        self.assertTrue(any("taking a break" in t for _, t in self.sent))

    def test_start_normally_stays_conversational(self):
        # No maintenance: /start is NOT consumed — falls through to the AI.
        ok = ron_daemon.handle_command(1, OTHER, "start", True,
                                       "/start", "Bob")
        self.assertFalse(ok)
        self.assertEqual(self.sent, [])

    # --- update gate -----------------------------------------------------
    def _fake_update(self, user_id, text):
        return {"message": {
            "message_id": 1,
            "from": {"id": user_id, "first_name": "X"},
            "chat": {"id": 5, "type": "private"},
            "text": text,
        }}

    def test_gate_silences_nonowner_chat(self):
        ron_daemon.set_maintenance(True, OWNER)
        ron_daemon.handle_update(self._fake_update(OTHER, "hello ron"))
        self.assertEqual(self.sent, [])  # silent: no reply...
        self.assertEqual(self.ai_calls, [])  # ...and no AI call burned

    def test_gate_allows_owner_chat(self):
        ron_daemon.set_maintenance(True, OWNER)
        ron_daemon.handle_update(self._fake_update(OWNER, "hello ron"))
        # Owner still gets the full flow (stubbed AI here).
        self.assertEqual(len(self.ai_calls), 1)

    def test_gate_allows_nonowner_start_command(self):
        ron_daemon.set_maintenance(True, OWNER)
        ron_daemon.handle_update(self._fake_update(OTHER, "/start"))
        self.assertTrue(any("taking a break" in t for _, t in self.sent))


if __name__ == "__main__":
    unittest.main()
