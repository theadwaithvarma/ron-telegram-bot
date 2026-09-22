#!/usr/bin/env python3
"""Business connection self-healing tests (v1.24.2).

Covers _recover_business_connection: a business_message arriving for a
connection id with no stored business_connection update must recover via
getBusinessConnection instead of being ignored forever.

No network, no Telegram. Run: python3 tests/test_business_recovery.py
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_daemon

CONN_ID = "test_conn_abc123"

GOOD_BC = {
    "id": CONN_ID,
    "user": {"id": 6819786239, "first_name": "Adwaith"},
    "user_chat_id": 6819786239,
    "date": 1789908085,
    "is_enabled": True,
    "can_reply": True,
    "rights": {"can_reply": True, "can_read_messages": True},
}


class BusinessRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_file = ron_daemon.BUSINESS_CONNECTIONS_FILE
        ron_daemon.BUSINESS_CONNECTIONS_FILE = os.path.join(
            self.tmp.name, "business_connections.json")
        # handle_business_message() also tracks staleness — keep the
        # fixture out of the LIVE business_stale.json (cf. AGENTS.md:
        # every test that can trigger _stale_note_* must redirect it).
        self.orig_stale = ron_daemon.BUSINESS_STALE_FILE
        ron_daemon.BUSINESS_STALE_FILE = os.path.join(
            self.tmp.name, "business_stale.json")
        # The negative-recovery cache is module-global; clear it so one
        # test's failed lookup doesn't suppress the next test's API call.
        ron_daemon._BC_RECOVERY_MISS.clear()
        self.orig_api = ron_daemon.api
        self.orig_ai = ron_daemon.ai_reply
        self.ai_calls = []
        ron_daemon.ai_reply = lambda *a, **k: self.ai_calls.append((a, k))

    def tearDown(self):
        ron_daemon.BUSINESS_CONNECTIONS_FILE = self.orig_file
        ron_daemon.BUSINESS_STALE_FILE = self.orig_stale
        ron_daemon._BC_RECOVERY_MISS.clear()
        ron_daemon.api = self.orig_api
        ron_daemon.ai_reply = self.orig_ai
        self.tmp.cleanup()

    def _bc_message(self):
        return {
            "business_connection_id": CONN_ID,
            "chat": {"id": 555, "type": "private"},
            "message_id": 42,
            "text": "hello, do you deliver?",
            "from": {"id": 111222333, "first_name": "Cust",
                     "username": "cust1"},
        }

    def test_recover_enabled_connection(self):
        ron_daemon.api = lambda m, p=None, timeout=65: (
            {"ok": True, "result": GOOD_BC}
            if m == "getBusinessConnection" else {"ok": False})
        conn = ron_daemon._recover_business_connection(CONN_ID)
        self.assertIsNotNone(conn)
        self.assertTrue(conn["can_reply"])
        self.assertEqual(conn["user_id"], 6819786239)
        # Persisted in the same shape handle_business_connection uses.
        with open(ron_daemon.BUSINESS_CONNECTIONS_FILE) as f:
            stored = json.load(f)
        self.assertIn(CONN_ID, stored)
        self.assertTrue(stored[CONN_ID]["can_reply"])

    def test_recover_failed_connection_cached_5min(self):
        # A dead connection id must not trigger an API call on every
        # business message for 5 minutes (negative cache).
        calls = []
        ron_daemon.api = lambda m, p=None, timeout=65: (
            calls.append(m) or {"ok": False,
                                "error": "Bad Request: connection not found"})
        self.assertIsNone(ron_daemon._recover_business_connection(CONN_ID))
        self.assertEqual(calls, ["getBusinessConnection"])
        # Second attempt within the window: served from cache, no API.
        self.assertIsNone(ron_daemon._recover_business_connection(CONN_ID))
        self.assertEqual(calls, ["getBusinessConnection"])
        # After the TTL expires, it tries the API again.
        with mock.patch.object(ron_daemon.time, "time",
                               return_value=ron_daemon.time.time() + 301):
            self.assertIsNone(
                ron_daemon._recover_business_connection(CONN_ID))
        self.assertEqual(calls, ["getBusinessConnection"] * 2)

    def test_recover_api_error_returns_none(self):
        ron_daemon.api = lambda m, p=None, timeout=65: {
            "ok": False, "error": "Bad Request: connection not found"}
        self.assertIsNone(ron_daemon._recover_business_connection(CONN_ID))
        self.assertFalse(os.path.exists(ron_daemon.BUSINESS_CONNECTIONS_FILE))

    def test_recover_disabled_connection_returns_none(self):
        bc = dict(GOOD_BC, is_enabled=False)
        ron_daemon.api = lambda m, p=None, timeout=65: {
            "ok": True, "result": bc}
        self.assertIsNone(ron_daemon._recover_business_connection(CONN_ID))

    def test_unknown_connection_message_triggers_recovery(self):
        calls = []
        ron_daemon.api = lambda m, p=None, timeout=65: (
            calls.append(m) or {"ok": True, "result": GOOD_BC})
        ron_daemon.handle_business_message(self._bc_message())
        self.assertIn("getBusinessConnection", calls)
        # Recovered connection -> message answered via normal AI flow.
        self.assertEqual(len(self.ai_calls), 1)
        _a, kw = self.ai_calls[0]
        self.assertEqual(kw.get("business_connection_id"), CONN_ID)

    def test_unrecoverable_connection_still_ignored(self):
        ron_daemon.api = lambda m, p=None, timeout=65: {"ok": False}
        ron_daemon.handle_business_message(self._bc_message())
        self.assertEqual(self.ai_calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
