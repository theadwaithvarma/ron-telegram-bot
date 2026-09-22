#!/usr/bin/env python3
"""Regression tests for the v1.27.3 reaction-targeting correction (no network).

Adwaith's spec: a [react:] trailer's reaction must land on HIS message —
the incoming user's message_id — and NEVER on the bot's own just-sent
reply. (v1.27.2 misread this and targeted the message he had replied to;
this reverts that logic.)

Fix under test: ai_reply receives the incoming message's id as
incoming_msg_id, threads it through stream_ai_reply -> _stream_with_draft
-> send_rich_with_kb -> _fire_media_trailers -> fire_actions -> _fire_one
as react_target_msg_id, and the [react:] action targets exactly that.
No incoming id -> the reaction is dropped (logged), never retargeted to
the bot's own reply.

Run: python3 tests/test_react_target.py
"""
import os
import sys
import unittest

REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, REPO)

# NOTE: ron_daemon inserts the LIVE dir (~/workspace/telegram-bot) at
# sys.path[0] on import, so if an alphabetically-earlier test module
# imported ron_daemon first, sys.modules already pins the LIVE
# ron_media_trailers. Evict it and re-import the repo copy, then rewire
# the daemon's module global so _fire_media_trailers uses it too.
sys.modules.pop("ron_media_trailers", None)
sys.path.insert(0, REPO)
import ron_media_trailers
import ron_daemon
ron_daemon.ron_media_trailers = ron_media_trailers
assert "ron-telegram-bot" in ron_media_trailers.__file__, \
    ron_media_trailers.__file__


class FakeApi:
    """Records setMessageReaction calls, answers ok."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, params):
        self.calls.append((method, params))
        return {"ok": True, "result": True}


class ReactTargetUnit(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self._orig = ron_media_trailers.rich_send.api_json
        ron_media_trailers.rich_send.api_json = self.api

    def tearDown(self):
        ron_media_trailers.rich_send.api_json = self._orig

    def _react_action(self, emoji="👍"):
        actions = ron_media_trailers.parse_media_trailers(
            f"nice one [react: {emoji}]")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["kind"], "react")
        return actions

    def test_react_targets_user_message_not_own_reply(self):
        # The correction: incoming user message is 777, bot's own reply is
        # 555 — the reaction must land on 777 (Adwaith's message), never
        # on 555.
        n = ron_media_trailers.fire_actions(
            self._react_action(), 1234, sent_msg_id=555,
            react_target_msg_id=777)
        self.assertEqual(n, 1)
        self.assertEqual(len(self.api.calls), 1)
        method, params = self.api.calls[0]
        self.assertEqual(method, "setMessageReaction")
        self.assertEqual(params["message_id"], 777)
        self.assertEqual(params["chat_id"], 1234)

    def test_react_never_falls_back_to_own_reply(self):
        # No incoming id: the reaction is dropped, NOT retargeted to the
        # bot's own reply.
        n = ron_media_trailers.fire_actions(
            self._react_action("🔥"), 1234, sent_msg_id=555,
            react_target_msg_id=None)
        self.assertEqual(n, 0)
        self.assertEqual(self.api.calls, [])

    def test_react_dropped_with_no_target(self):
        n = ron_media_trailers.fire_actions(
            self._react_action(), 1234, sent_msg_id=None,
            react_target_msg_id=None)
        self.assertEqual(n, 0)
        self.assertEqual(self.api.calls, [])


class ReactTargetHelpers(unittest.TestCase):
    def test_react_target_for(self):
        self.assertEqual(ron_daemon._react_target_for(777), 777)
        self.assertIsNone(ron_daemon._react_target_for(None))
        self.assertIsNone(ron_daemon._react_target_for(0))
        self.assertIsNone(ron_daemon._react_target_for(""))


class ReactTargetPlumbing(unittest.TestCase):
    """send_rich_with_kb and _fire_media_trailers forward the target."""

    def test_send_rich_with_kb_forwards_target(self):
        seen = {}

        def fake_fire(reply, chat_id, sent_r, reply_to=None, thread_id=None,
                      business_connection_id=None, react_target_msg_id=None):
            seen["react_target_msg_id"] = react_target_msg_id
            seen["sent_r"] = sent_r

        orig_fire = ron_daemon._fire_media_trailers
        orig_send = ron_daemon.send_rich
        ron_daemon._fire_media_trailers = fake_fire
        # Force the classic path to return ok with a fake message id.
        ron_daemon.send_rich = lambda *a, **k: {
            "ok": True, "result": {"message_id": 555}}
        try:
            r = ron_daemon.send_rich_with_kb(
                1234, "done [react: 👍]", reply_to=111,
                react_target_msg_id=777)
            self.assertTrue(r.get("ok"))
            self.assertEqual(seen.get("react_target_msg_id"), 777)
            self.assertEqual(
                seen["sent_r"]["result"]["message_id"], 555)
        finally:
            ron_daemon._fire_media_trailers = orig_fire
            ron_daemon.send_rich = orig_send

    def test_fire_media_trailers_forwards_target(self):
        seen = {}
        orig = ron_media_trailers.fire_actions

        def fake_fire(actions, chat_id, reply_to=None, thread_id=None,
                      business_connection_id=None, sent_msg_id=None,
                      react_target_msg_id=None):
            seen["react_target_msg_id"] = react_target_msg_id
            seen["sent_msg_id"] = sent_msg_id
            return 1

        ron_media_trailers.fire_actions = fake_fire
        try:
            ron_daemon._fire_media_trailers(
                "done [react: 👍]", 1234,
                {"result": {"message_id": 555}},
                react_target_msg_id=777)
            self.assertEqual(seen.get("react_target_msg_id"), 777)
            self.assertEqual(seen.get("sent_msg_id"), 555)
        finally:
            ron_media_trailers.fire_actions = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
