#!/usr/bin/env python3
"""Regression tests for the v1.27.1 duplicate-bubble fix (no network).

Bug: on an edit turn (user replies to the bot's message with "make it
shorter"), pressing Stop mid-stream truncated the reply before the
trailing [edit] trailer streamed. _stream_with_draft then fell through to
the normal send and posted the truncated fragment ("TL;DR - AI Models ...")
as a NEW bubble next to the in-place edit.

Fix under test: edit turns never stream drafts, and a stopped edit turn
sends nothing at all. Non-edit turns keep the old behavior (stopped
partial still lands as a message).

Run: python3 tests/test_edit_nodup.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_daemon


class StreamHarness(unittest.TestCase):
    def setUp(self):
        self.typing_calls = []
        self.draft_calls = []
        self.send_calls = []
        self._orig_typing = ron_daemon.send_typing
        self._orig_draft = ron_daemon.draft_update
        self._orig_send = ron_daemon.send_rich_with_kb
        ron_daemon.send_typing = lambda chat_id: self.typing_calls.append(
            chat_id)
        ron_daemon.draft_update = lambda chat_id, draft_id, text: \
            self.draft_calls.append((chat_id, draft_id, text)) or (True, 0)
        ron_daemon.send_rich_with_kb = self._fake_send
        # Make draft pushes deterministic: every token pushes.
        self._orig_push_secs = ron_daemon.DRAFT_PUSH_SECS
        self._orig_min_growth = ron_daemon.DRAFT_MIN_GROWTH
        ron_daemon.DRAFT_PUSH_SECS = 0
        ron_daemon.DRAFT_MIN_GROWTH = 1

    def tearDown(self):
        ron_daemon.send_typing = self._orig_typing
        ron_daemon.draft_update = self._orig_draft
        ron_daemon.send_rich_with_kb = self._orig_send
        ron_daemon.DRAFT_PUSH_SECS = self._orig_push_secs
        ron_daemon.DRAFT_MIN_GROWTH = self._orig_min_growth
        ron_daemon.STOP_EVENTS.pop(424242, None)

    def _fake_send(self, chat_id, reply, reply_to=None, thread_id=None,
                   business_connection_id=None, user_text="",
                   edit_target_msg_id=None):
        self.send_calls.append({
            "reply": reply, "edit_target_msg_id": edit_target_msg_id})
        return {"ok": True}

    def _run(self, produce, edit_target_msg_id=None):
        return ron_daemon._stream_with_draft(
            424242, produce, user_text="make it shorter",
            edit_target_msg_id=edit_target_msg_id)


def _produce_full_edit(on_token, stop_event):
    for tok in ["Shorter version", "\n[edit]"]:
        on_token(tok)
    return "Shorter version\n[edit]"


def _produce_stopped_edit(on_token, stop_event):
    # User pressed Stop mid-stream: partial reply, [edit] never streamed.
    on_token("TL;DR - AI Models ")
    on_token("...")
    stop_event.set()
    return "TL;DR - AI Models ..."


def _produce_normal(on_token, stop_event):
    on_token("Hello there, ")
    on_token("how can I help?")
    return "Hello there, how can I help?"


def _produce_stopped_normal(on_token, stop_event):
    on_token("Hello there, ")
    stop_event.set()
    return "Hello there, "


class TestEditTurnNoDraftsNoDuplicates(StreamHarness):
    def test_edit_turn_sends_zero_drafts(self):
        self._run(_produce_full_edit, edit_target_msg_id=77)
        self.assertEqual(self.draft_calls, [],
                         "edit turn must not push any draft")

    def test_edit_turn_single_final_send_with_target(self):
        self._run(_produce_full_edit, edit_target_msg_id=77)
        self.assertEqual(len(self.send_calls), 1)
        self.assertEqual(self.send_calls[0]["edit_target_msg_id"], 77)
        self.assertIn("[edit]", self.send_calls[0]["reply"])

    def test_stopped_edit_turn_sends_nothing(self):
        # The v1.27.0 bug: partial fell through to a normal send and
        # posted a duplicate truncated bubble.
        reply = self._run(_produce_stopped_edit, edit_target_msg_id=77)
        self.assertEqual(reply, "TL;DR - AI Models ...")
        self.assertEqual(self.send_calls, [],
                         "stopped edit turn must not send any message")
        self.assertEqual(self.draft_calls, [])

    def test_stopped_edit_turn_still_typing(self):
        self._run(_produce_stopped_edit, edit_target_msg_id=77)
        self.assertEqual(self.typing_calls, [424242])


class TestNonEditTurnUnchanged(StreamHarness):
    def test_normal_turn_streams_drafts_and_sends(self):
        self._run(_produce_normal)
        self.assertTrue(len(self.draft_calls) >= 2,
                        "non-edit turns still stream drafts")
        self.assertEqual(len(self.send_calls), 1)
        self.assertIsNone(self.send_calls[0]["edit_target_msg_id"])

    def test_stopped_normal_turn_still_sends_partial(self):
        # Pre-existing behavior for non-edit turns is preserved: a
        # stopped partial lands as a message (only edit turns suppress).
        self._run(_produce_stopped_normal)
        self.assertEqual(len(self.send_calls), 1)
        self.assertEqual(self.send_calls[0]["reply"], "Hello there, ")


if __name__ == "__main__":
    unittest.main()
