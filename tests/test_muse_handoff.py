#!/usr/bin/env python3
"""/muse handoff (v1.32.0) tests: owner-only + DM-only gating, reply_msg
context capture, atomic queue writes, claim/done markers, duplicate
suppression, and crash/restart recovery of the response outbox.

No network, no Telegram. Run: python3 tests/test_muse_handoff.py
(from ~/workspace/ron-telegram-bot)
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)


def _load_repo_module(name):
    """Load the REPO's <name>.py as the canonical module object.

    ron_daemon.py puts the live dir first on sys.path (its line-27 tweak),
    so whichever test module imports the daemon first caches the LIVE
    copy in sys.modules. Register the repo copies first so this file —
    and the daemon it loads — test repo code, not deployed code.
    """
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(REPO, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    assert "ron-telegram-bot" in mod.__file__, \
        f"testing live copy of {name}: {mod.__file__}"
    return mod


ron_muse_handoff = _load_repo_module("ron_muse_handoff")
ron_daemon = _load_repo_module("ron_daemon")

OWNER = ron_daemon.OWNER_ID
OTHER = 999888777  # a random non-owner user


class HandoffStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hd = ron_muse_handoff
        self.hd.set_dir(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_submit_writes_atomic_json(self):
        rid = self.hd.submit_request(111, 222, OWNER, "Adwaith",
                                     "summarize this", reply_msg=None)
        path = os.path.join(self.tmp.name, "handoff", "requests",
                            rid + ".json")
        self.assertTrue(os.path.isfile(path))
        # mode 0600: owner read/write only
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with open(path) as f:
            payload = json.load(f)
        self.assertEqual(payload["chat_id"], 111)
        self.assertEqual(payload["message_id"], 222)
        self.assertEqual(payload["user_id"], OWNER)
        self.assertEqual(payload["command_text"], "summarize this")

    def test_submit_captures_reply_context(self):
        reply_msg = {
            "message_id": 555,
            "date": 1758570000,
            "from": {"id": 777, "first_name": "Soul",
                     "last_name": "Dragon", "username": "souldragon"},
            "text": "check this out",
            "photo": [{"file_id": "x"}],
        }
        rid = self.hd.submit_request(111, 222, OWNER, "Adwaith",
                                     "reply to this", reply_msg=reply_msg)
        path = os.path.join(self.tmp.name, "handoff", "requests",
                            rid + ".json")
        with open(path) as f:
            payload = json.load(f)
        rc = payload["reply_context"]
        self.assertEqual(rc["message_id"], 555)
        self.assertEqual(rc["from_id"], 777)
        self.assertEqual(rc["from_username"], "souldragon")
        self.assertEqual(rc["date"], 1758570000)
        self.assertEqual(rc["text"], "check this out")
        self.assertTrue(rc["has_photo"])

    def test_pending_skips_claimed_and_done(self):
        r1 = self.hd.submit_request(1, 1, OWNER, "A", "one")
        r2 = self.hd.submit_request(1, 2, OWNER, "A", "two")
        r3 = self.hd.submit_request(1, 3, OWNER, "A", "three")
        self.hd.claim_request(r2)
        req_dir = os.path.join(self.tmp.name, "handoff", "requests")
        open(os.path.join(req_dir, r3 + ".done"), "a").close()
        pending = self.hd.pending_requests()
        self.assertEqual([p["id"] for p in pending], [r1])

    def test_claim_is_idempotent(self):
        rid = self.hd.submit_request(1, 1, OWNER, "A", "one")
        self.hd.claim_request(rid)
        self.hd.claim_request(rid)  # no error
        self.assertEqual(self.hd.pending_requests(), [])

    def test_response_roundtrip_and_drain_cleanup(self):
        rid = self.hd.submit_request(111, 222, OWNER, "A", "ping")
        self.hd.write_response(rid, "pong")
        drained = self.hd.drain_responses()
        self.assertEqual(len(drained), 1)
        req, resp = drained[0]
        self.assertEqual(req["id"], rid)
        self.assertEqual(resp["reply_text"], "pong")
        # response file removed; second drain is empty (no double-send)
        res_dir = os.path.join(self.tmp.name, "handoff", "responses")
        self.assertEqual(os.listdir(res_dir), [])
        self.assertEqual(self.hd.drain_responses(), [])
        # request marked done: never re-wakes
        self.assertEqual(self.hd.pending_requests(), [])

    def test_drain_tolerates_orphan_response(self):
        # response file with no matching request (crash leftovers)
        self.hd.write_response("no-such-request", "hello")
        drained = self.hd.drain_responses()
        self.assertEqual(len(drained), 1)
        self.assertIsNone(drained[0][0])
        self.assertEqual(drained[0][1]["reply_text"], "hello")


class MuseCommandTest(unittest.TestCase):
    """handle_command(cmd='muse') gating and queueing."""

    def setUp(self):
        self.sent = []
        self.submitted = []
        d = ron_daemon
        self._real_send = d.send
        self._real_handoff = d.ron_muse_handoff

        def fake_send(chat_id, text, reply_to=None, thread_id=None,
                      business_connection_id=None):
            self.sent.append({"chat_id": chat_id, "text": text})
            return {"ok": True, "result": {"message_id": 1}}

        class FakeHandoff:
            def submit_request(h_self, chat_id, message_id, user_id,
                               user_name, command_text, reply_msg=None):
                self.submitted.append({
                    "chat_id": chat_id, "message_id": message_id,
                    "user_id": user_id, "user_name": user_name,
                    "command_text": command_text, "reply_msg": reply_msg})
                return "req-1"

        d.send = fake_send
        d.ron_muse_handoff = FakeHandoff()

    def tearDown(self):
        ron_daemon.send = self._real_send
        ron_daemon.ron_muse_handoff = self._real_handoff

    def _call(self, user_id, is_private=True, text="/muse do the thing",
              reply_msg=None):
        return ron_daemon.handle_command(
            111, user_id, "muse", is_private, text=text,
            user_name="Adwaith", cmd_msg_id=999, reply_msg=reply_msg)

    def test_non_owner_refused(self):
        self.assertTrue(self._call(OTHER))
        self.assertEqual(self.submitted, [])
        self.assertTrue(any("Only Adwaith" in s["text"] for s in self.sent))

    def test_owner_group_refused(self):
        self.assertTrue(self._call(OWNER, is_private=False))
        self.assertEqual(self.submitted, [])
        self.assertTrue(any("DM" in s["text"] for s in self.sent))

    def test_empty_args_usage(self):
        self.assertTrue(self._call(OWNER, text="/muse"))
        self.assertEqual(self.submitted, [])
        self.assertTrue(any("Usage" in s["text"] for s in self.sent))

    def test_owner_dm_queues_and_acks(self):
        reply_msg = {"message_id": 5, "text": "context here",
                     "from": {"id": 7, "first_name": "X"}}
        self.assertTrue(self._call(OWNER, reply_msg=reply_msg))
        self.assertEqual(len(self.submitted), 1)
        sub = self.submitted[0]
        self.assertEqual(sub["user_id"], OWNER)
        self.assertEqual(sub["command_text"], "do the thing")
        self.assertIs(sub["reply_msg"], reply_msg)
        self.assertTrue(any("Pinged Muse" in s["text"] for s in self.sent))

    def test_queue_failure_is_surfaced(self):
        class Boom:
            def submit_request(self, *a, **k):
                raise OSError("disk full")
        ron_daemon.ron_muse_handoff = Boom()
        self.assertTrue(self._call(OWNER))
        self.assertTrue(any("Couldn't queue" in s["text"]
                            for s in self.sent))


if __name__ == "__main__":
    unittest.main()
