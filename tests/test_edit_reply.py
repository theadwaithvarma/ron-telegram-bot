#!/usr/bin/env python3
"""Offline tests for v1.27.0 user-requested message editing (no network).

Run: python3 tests/test_edit_reply.py  (or python3 -m unittest discover tests)

Covers: [edit] trailer parsing (bare vs colon form), strip_trailer_syntax
removal, _is_own_message gating (bot username, other bots, humans,
business-mode OWNER_ID), _edit_context extraction, and the
send_rich_with_kb edit flow: success edits in place with no new message,
"message is not modified" is a silent no-op, and genuine edit failures
fall back to a normal new reply. No network calls, no state files touched.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_edit_reply as er
import ron_daemon


def _msg(mid, from_id, username=None, is_bot=False, text=None, caption=None):
    m = {"message_id": mid,
         "from": {"id": from_id, "is_bot": is_bot}}
    if username:
        m["from"]["username"] = username
    if text is not None:
        m["text"] = text
    if caption is not None:
        m["caption"] = caption
    return m


class TestParseEditTrailer(unittest.TestCase):
    def test_bare(self):
        self.assertTrue(er.has_edit_trailer("Shorter version\n[edit]"))

    def test_bare_case_insensitive(self):
        self.assertTrue(er.has_edit_trailer("x [EDIT]"))

    def test_colon_form(self):
        self.assertTrue(er.has_edit_trailer("[edit: fixed text here]"))

    def test_none(self):
        self.assertFalse(er.has_edit_trailer("just a normal reply"))
        self.assertFalse(er.has_edit_trailer(""))
        self.assertFalse(er.has_edit_trailer(None))

    def test_extract_colon_body(self):
        self.assertEqual(er.extract_revised_text("[edit: fixed text here]"),
                         "fixed text here")

    def test_extract_colon_multiline(self):
        self.assertEqual(
            er.extract_revised_text("[edit: line one\nline two]"),
            "line one\nline two")

    def test_extract_bare_returns_empty(self):
        # Bare [edit]: caller takes the whole reply minus all trailers.
        self.assertEqual(er.extract_revised_text("Shorter\n[edit]"), "")

    def test_extract_none(self):
        self.assertIsNone(er.extract_revised_text("no trailer here"))


class TestStripEditTrailer(unittest.TestCase):
    def test_strip_bare(self):
        self.assertEqual(ron_daemon.strip_trailer_syntax("hi\n[edit]"), "hi")

    def test_strip_colon(self):
        self.assertEqual(
            ron_daemon.strip_trailer_syntax("[edit: new text]"), "")

    def test_strip_mid_line(self):
        self.assertEqual(
            ron_daemon.strip_trailer_syntax("keep this [edit]"), "keep this")


class TestIsOwnMessage(unittest.TestCase):
    def test_own_bot_message(self):
        m = _msg(11, 999, username=ron_daemon.BOT_USERNAME, is_bot=True,
                 text="hello")
        self.assertTrue(ron_daemon._is_own_message(m))

    def test_other_bot_rejected(self):
        m = _msg(11, 123, username="someotherbot", is_bot=True, text="hi")
        self.assertFalse(ron_daemon._is_own_message(m))

    def test_human_rejected(self):
        m = _msg(11, 456, username="human", is_bot=False, text="hi")
        self.assertFalse(ron_daemon._is_own_message(m))

    def test_business_owner_accepted(self):
        m = _msg(11, ron_daemon.OWNER_ID, is_bot=False, text="hi")
        self.assertTrue(ron_daemon._is_own_message(m, "conn123"))

    def test_business_stranger_rejected(self):
        m = _msg(11, 777, is_bot=False, text="hi")
        self.assertFalse(ron_daemon._is_own_message(m, "conn123"))

    def test_none_safe(self):
        self.assertFalse(ron_daemon._is_own_message(None))
        self.assertFalse(ron_daemon._is_own_message({}))


class TestEditContext(unittest.TestCase):
    def test_extracts_id_and_text(self):
        m = _msg(42, 999, username=ron_daemon.BOT_USERNAME, is_bot=True,
                 text="original message")
        mid, text = ron_daemon._edit_context(m)
        self.assertEqual(mid, 42)
        self.assertEqual(text, "original message")

    def test_caption_fallback(self):
        m = _msg(42, 999, username=ron_daemon.BOT_USERNAME, is_bot=True,
                 caption="photo caption")
        mid, text = ron_daemon._edit_context(m)
        self.assertEqual((mid, text), (42, "photo caption"))

    def test_not_own_message(self):
        m = _msg(42, 456, is_bot=False, text="human text")
        self.assertEqual(ron_daemon._edit_context(m), (None, ""))

    def test_missing_message_id(self):
        m = {"from": {"id": 999, "is_bot": True,
                      "username": ron_daemon.BOT_USERNAME},
             "text": "x"}
        self.assertEqual(ron_daemon._edit_context(m), (None, ""))

    def test_long_text_truncated(self):
        m = _msg(1, 999, username=ron_daemon.BOT_USERNAME, is_bot=True,
                 text="x" * 5000)
        _, text = ron_daemon._edit_context(m)
        self.assertEqual(len(text), 1500)


class TestEditFlow(unittest.TestCase):
    """send_rich_with_kb with edit_target_msg_id: stub send_healed."""

    def setUp(self):
        self.calls = []
        self._orig = ron_daemon.send_healed

        def fake(method, params, as_json=False):
            self.calls.append((method, dict(params)))
            return self._script.pop(0)

        ron_daemon.send_healed = fake

    def tearDown(self):
        ron_daemon.send_healed = self._orig

    def _run(self, reply, script):
        self._script = list(script)
        return ron_daemon.send_rich_with_kb(
            12345, reply, edit_target_msg_id=77)

    def test_edit_success_no_new_message(self):
        r = self._run("Shorter version\n[edit]",
                      [{"ok": True, "result": {"message_id": 77}}])
        self.assertEqual(r, {"ok": True, "edited": True})
        # Exactly one API call: the edit. No new message was sent.
        self.assertEqual(len(self.calls), 1)
        method, params = self.calls[0]
        self.assertEqual(method, "editMessageText")
        self.assertEqual(params["message_id"], 77)
        self.assertEqual(params["text"], "Shorter version")
        self.assertNotIn("[edit]", params["text"])

    def test_edit_uses_colon_body(self):
        r = self._run("[edit: brand new text]",
                      [{"ok": True, "result": {"message_id": 77}}])
        self.assertEqual(r, {"ok": True, "edited": True})
        _, params = self.calls[0]
        self.assertEqual(params["text"], "brand new text")

    def test_not_modified_is_silent_success(self):
        r = self._run("same\n[edit]",
                      [{"ok": False,
                        "error": "Bad Request: message is not modified"}])
        self.assertEqual(r, {"ok": True, "edited": True})
        # No fallback send — nothing to change.
        self.assertEqual(len(self.calls), 1)

    def test_edit_failure_falls_back_to_new_reply(self):
        r = self._run("Revised text\n[edit]", [
            {"ok": False,
             "error": "Bad Request: message can't be edited"},
            {"ok": True, "result": {"message_id": 99}},
        ])
        # Fallback: a normal send of the revised text (trailer stripped).
        self.assertTrue(r.get("ok"))
        self.assertNotIn("edited", r)
        methods = [c[0] for c in self.calls]
        self.assertEqual(methods[0], "editMessageText")
        self.assertIn(methods[1], ("sendRichMessage", "sendMessage"))
        sent_text = self.calls[1][1].get("text") or \
            (self.calls[1][1].get("rich_message") or {}).get("markdown")
        self.assertIn("Revised text", sent_text)
        self.assertNotIn("[edit]", sent_text)

    def test_no_edit_trailer_normal_send(self):
        r = self._run("Just a normal reply",
                      [{"ok": True, "result": {"message_id": 99}}])
        self.assertTrue(r.get("ok"))
        self.assertNotIn("edited", r)
        self.assertNotEqual(self.calls[0][0], "editMessageText")

    def test_no_target_normal_send(self):
        # [edit] trailer but no valid target: plain reply, trailer stripped.
        self._script = [{"ok": True, "result": {"message_id": 99}}]
        orig = ron_daemon.send_healed
        calls = []
        ron_daemon.send_healed = lambda m, p, as_json=False: (
            calls.append((m, dict(p))),
            self._script.pop(0))[1]
        try:
            r = ron_daemon.send_rich_with_kb(12345, "Hi\n[edit]")
        finally:
            ron_daemon.send_healed = orig
        self.assertTrue(r.get("ok"))
        self.assertNotEqual(calls[0][0], "editMessageText")


if __name__ == "__main__":
    unittest.main()
