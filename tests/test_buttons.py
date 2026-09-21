#!/usr/bin/env python3
"""Offline tests for v1.21.0 new Telegram button types (no network).

Run: python3 tests/test_buttons.py  (or python3 -m unittest discover tests)

Covers:
- [copy: Label | text] trailer -> copy_text inline button (line + tail
  forms), official 1-256 char cap, malformed pairs dropped, overlap with
  the contextual _button_fits_context filter.
- [reply_keyboard: a, b, c] -> ReplyKeyboardMarkup (resize + one_time,
  3 per row), [hide_keyboard] -> ReplyKeyboardRemove, inline keyboard
  wins when both are present.
- [switch_inline: Label | query] -> switch_inline_query button.
- [login: Label] -> login_url button; dropped loudly when ron_ai.json
  "login_url" is unconfigured.
- strip_trailer_syntax removes every new trailer form.
- Validator: copy_text/login_url shapes, ReplyKeyboardMarkup and
  ReplyKeyboardRemove markup, unknown markup kinds flagged.
- callback_data 64-byte rule unaffected.
- handle_inline_query: 2 article results with a query, 1 without.
- Prompt sync: repo DEFAULT_CONFIG and live ron_ai.json carry the same
  new-button-types paragraph.
"""
import importlib.util
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)


def _load_repo_module(name):
    """Force the REPO copy (ron_daemon puts the live dir first on sys.path;
    same pattern as tests/test_powercmds.py)."""
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(REPO, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ron_owner = _load_repo_module("ron_owner")
ron_daemon = _load_repo_module("ron_daemon")
ron_richvalidate = _load_repo_module("ron_richvalidate")


def _parse(reply):
    with redirect_stdout(io.StringIO()):
        return ron_daemon.parse_trailers(reply)


class TestCopyTrailer(unittest.TestCase):
    def test_copy_line_form(self):
        clean, kb = _parse(
            "Here's your invite code: ABC123\n"
            "[copy: Copy invite code | ABC123]")
        self.assertNotIn("[copy:", clean)
        self.assertEqual(kb, {"inline_keyboard": [
            [{"text": "Copy invite code",
              "copy_text": {"text": "ABC123"}}]]})

    def test_copy_tail_form(self):
        clean, kb = _parse(
            "Your code is ABC123. [copy: Copy invite code | ABC123]")
        self.assertNotIn("[copy:", clean)
        row = kb["inline_keyboard"][0]
        self.assertEqual(row[0]["copy_text"], {"text": "ABC123"})

    def test_copy_text_capped_256(self):
        clean, kb = _parse(
            "Your long code is ready.\n"
            "[copy: Copy code | " + "x" * 400 + "]")
        got = kb["inline_keyboard"][0][0]["copy_text"]["text"]
        self.assertEqual(len(got), 256)

    def test_copy_malformed_pair_dropped(self):
        clean, kb = _parse("Hello\n[copy: just a label]")
        self.assertNotIn("[copy:", clean)
        self.assertIsNone(kb)

    def test_copy_passes_contextual_filter(self):
        # Label grounded in the reply's topic -> kept.
        clean, kb = _parse(
            "Your invite code ABC123 is ready\n"
            "[copy: Copy invite code | ABC123]")
        self.assertIsNotNone(kb)

    def test_copy_off_topic_dropped(self):
        # Generic copy button on an unrelated reply -> filtered like any
        # off-topic quick reply.
        clean, kb = _parse(
            "Valorant tips: aim for the head\n"
            "[copy: Copy code | xyz]")
        self.assertIsNone(kb)

    def test_copy_rides_rich_inbubble(self):
        # RichMessageButton takes the same actions as an inline keyboard
        # button (Bot API 10.3), including copy_text.
        blk = ron_daemon.rich_buttons_block(
            [{"text": "Copy invite code", "copy_text": {"text": "ABC123"}}])
        self.assertEqual(blk["buttons"][0]["copy_text"], {"text": "ABC123"})


class TestReplyKeyboard(unittest.TestCase):
    def test_reply_keyboard_shape(self):
        clean, kb = _parse(
            "Here are your shortcuts\n"
            "[reply_keyboard: /ping, /imagine, /health]")
        self.assertNotIn("[reply_keyboard:", clean)
        self.assertEqual(kb, {"keyboard": [
            [{"text": "/ping"}, {"text": "/imagine"}, {"text": "/health"}]],
            "resize_keyboard": True,
            "one_time_keyboard": True})

    def test_reply_keyboard_rows_of_three(self):
        clean, kb = _parse("[reply_keyboard: a, b, c, d]")
        self.assertEqual(kb["keyboard"],
                        [[{"text": "a"}, {"text": "b"}, {"text": "c"}],
                         [{"text": "d"}]])

    def test_hide_keyboard(self):
        clean, kb = _parse("Menu dismissed\n[hide_keyboard]")
        self.assertNotIn("[hide_keyboard]", clean)
        self.assertEqual(kb, {"remove_keyboard": True})

    def test_hide_keyboard_tail_form(self):
        clean, kb = _parse("Done. [hide_keyboard]")
        self.assertEqual(kb, {"remove_keyboard": True})

    def test_inline_keyboard_wins_over_reply(self):
        # One message can only carry ONE reply_markup kind.
        clean, kb = _parse(
            "Pick one\n[buttons: Yes]\n[reply_keyboard: /ping]")
        self.assertIn("inline_keyboard", kb)
        self.assertNotIn("keyboard", kb)

    def test_no_buttons_no_keyboard(self):
        clean, kb = _parse("Just text")
        self.assertIsNone(kb)


class TestSwitchInline(unittest.TestCase):
    def test_switch_inline_line_form(self):
        clean, kb = _parse(
            "Try Ron anywhere\n"
            "[switch_inline: Try Ron here | hello]")
        self.assertNotIn("[switch_inline:", clean)
        self.assertEqual(kb, {"inline_keyboard": [
            [{"text": "Try Ron here",
              "switch_inline_query": "hello"}]]})

    def test_switch_inline_tail_form(self):
        clean, kb = _parse(
            "Share me. [switch_inline: Try Ron | ]")
        self.assertEqual(
            kb["inline_keyboard"][0][0]["switch_inline_query"], "")

    def test_switch_inline_malformed_dropped(self):
        clean, kb = _parse("Hi\n[switch_inline: no query here]")
        self.assertNotIn("[switch_inline:", clean)
        self.assertIsNone(kb)


class TestLoginButton(unittest.TestCase):
    def _cfg(self, url):
        class Cfg:
            def get(self, k, d=None):
                return url if k == "login_url" else d
        return Cfg()

    def _with_config(self, url):
        return type("S", (), {
            "load_config": staticmethod(lambda: self._cfg(url))})()

    def test_login_dropped_when_unconfigured(self):
        real = ron_daemon.ron_ai
        ron_daemon.ron_ai = self._with_config("")
        try:
            clean, kb = _parse("Welcome\n[login: Log in]")
        finally:
            ron_daemon.ron_ai = real
        self.assertNotIn("[login:", clean)
        self.assertIsNone(kb)

    def test_login_present_when_configured(self):
        real = ron_daemon.ron_ai
        ron_daemon.ron_ai = self._with_config("https://ex.com/auth")
        try:
            clean, kb = _parse("Welcome\n[login: Log in]")
        finally:
            ron_daemon.ron_ai = real
        self.assertEqual(kb, {"inline_keyboard": [
            [{"text": "Log in",
              "login_url": {"url": "https://ex.com/auth"}}]]})


class TestStripNewTrailers(unittest.TestCase):
    def test_all_new_forms_stripped(self):
        reply = ("text\n[copy: L | t]\n[switch_inline: L | q]\n"
                 "[login: Log in]\n[reply_keyboard: a]\n[hide_keyboard]")
        self.assertEqual(ron_daemon.strip_trailer_syntax(reply), "text")
        # tail forms (tacked onto a sentence) strip too
        reply2 = "text [copy: L | t]"
        self.assertEqual(ron_daemon.strip_trailer_syntax(reply2), "text")


class TestButtonValidator(unittest.TestCase):
    def _v(self, params, method="sendMessage"):
        out = []
        ron_richvalidate._validate_send_message(params, method, out)
        return out

    def test_valid_copy_button(self):
        out = self._v({"text": "t", "reply_markup": {"inline_keyboard": [
            [{"text": "Copy", "copy_text": {"text": "ABC"}}]]}})
        self.assertEqual(out, [])

    def test_copy_text_too_long(self):
        out = self._v({"text": "t", "reply_markup": {"inline_keyboard": [
            [{"text": "Copy", "copy_text": {"text": "x" * 300}}]]}})
        self.assertTrue(any("copy_text.text" in e for e in out), out)

    def test_valid_login_url(self):
        out = self._v({"text": "t", "reply_markup": {"inline_keyboard": [
            [{"text": "Log in",
              "login_url": {"url": "https://ex.com/a"}}]]}})
        self.assertEqual(out, [])

    def test_login_url_missing_url(self):
        out = self._v({"text": "t", "reply_markup": {"inline_keyboard": [
            [{"text": "Log in", "login_url": {"forward_text": "x"}}]]}})
        self.assertTrue(any("login_url.url" in e for e in out), out)

    def test_valid_reply_keyboard(self):
        out = self._v({"text": "t", "reply_markup": {"keyboard": [
            [{"text": "/ping"}, {"text": "/imagine"}]],
            "resize_keyboard": True, "one_time_keyboard": True}})
        self.assertEqual(out, [])

    def test_reply_keyboard_empty_text_flagged(self):
        out = self._v({"text": "t", "reply_markup": {"keyboard": [
            [{"text": ""}]]}})
        self.assertTrue(any("text: required non-empty" in e for e in out),
                        out)

    def test_valid_keyboard_remove(self):
        out = self._v({"text": "t", "reply_markup": {"remove_keyboard": True}})
        self.assertEqual(out, [])

    def test_unknown_markup_kind_flagged(self):
        out = self._v({"text": "t", "reply_markup": {"force_reply": True}})
        self.assertTrue(any("unknown markup kind" in e for e in out), out)


class TestInlineQueryHandler(unittest.TestCase):
    def test_two_results_with_query(self):
        calls = []
        real = ron_daemon.api
        ron_daemon.api = lambda m, p=None, **k: calls.append((m, p)) or {"ok": True}
        try:
            ron_daemon.handle_inline_query({"id": "q1", "query": "hello"})
        finally:
            ron_daemon.api = real
        self.assertEqual(len(calls), 1)
        m, p = calls[0]
        self.assertEqual(m, "answerInlineQuery")
        self.assertEqual(p["inline_query_id"], "q1")
        results = json.loads(p["results"])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["title"], "Ask Ron: hello")
        self.assertEqual(results[0]["input_message_content"]["message_text"],
                         "hello")
        self.assertEqual(results[1]["title"], "Open Ron chat")

    def test_one_result_without_query(self):
        calls = []
        real = ron_daemon.api
        ron_daemon.api = lambda m, p=None, **k: calls.append((m, p)) or {"ok": True}
        try:
            ron_daemon.handle_inline_query({"id": "q2", "query": " "})
        finally:
            ron_daemon.api = real
        results = json.loads(calls[0][1]["results"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "openron")


class TestCallbackRuleUnaffected(unittest.TestCase):
    def test_callback_64byte_rule(self):
        # Multibyte labels still truncate by bytes, not chars.
        clean, kb = _parse("Code review notes\n[buttons: Review code "
                           + "🔥" * 30 + "]")
        row = kb["inline_keyboard"][0]
        n = len(row[0]["callback_data"].encode("utf-8"))
        self.assertLessEqual(n, 64)


class TestPromptSync(unittest.TestCase):
    def test_new_button_paragraph_in_sync(self):
        spec = importlib.util.spec_from_file_location(
            "ron_ai_repo", os.path.join(REPO, "ron_ai.py"))
        repo_ai = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(repo_ai)
        repo_sys = repo_ai.DEFAULT_CONFIG["system"]
        live = json.load(open(os.path.expanduser(
            "~/workspace/telegram-bot/ron_ai.json")))["system"]

        def grab(s):
            i = s.find("New button types.")
            j = s.find("never generic continuations. ") + \
                len("never generic continuations. ")
            self.assertTrue(i >= 0 and j > i)
            return s[i:j]
        self.assertEqual(grab(repo_sys), grab(live))


if __name__ == "__main__":
    unittest.main(verbosity=2)
