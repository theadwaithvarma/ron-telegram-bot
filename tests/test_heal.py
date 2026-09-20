#!/usr/bin/env python3
"""Self-healing send pipeline (v1.14.0) tests: the bot detects its own
formatting errors and fixes them automatically.

The Telegram API layer is mocked to fail in scripted sequences; each test
asserts the retry chain behaves:
  original -> fix matching the error -> the other fix -> give up (<=3 tries).

No network, no Telegram. Run: python3 -m unittest discover tests -v
(from ~/workspace/ron-telegram-bot)
"""
import contextlib
import importlib.util
import io
import os
import sys
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)


def _load_repo_module(name):
    """Load the REPO's <name>.py as the canonical module object.

    ron_daemon.py puts the live dir first on sys.path (its line-27 tweak),
    so whichever test module imports the daemon first caches the LIVE
    ron_owner/ron_daemon in sys.modules. Register the repo copies first so
    this file tests repo code, not deployed code.
    """
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(REPO, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ron_daemon = _load_repo_module("ron_daemon")
rich_send = _load_repo_module("rich_send")


class ScriptedAPI:
    """Mock transport: yields scripted results, records every call."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []  # (method, params)

    def __call__(self, method, params):
        self.calls.append((method, dict(params)))
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]  # last result repeats (persistent failure)


def _fail(err):
    return {"ok": False, "error": err}


_OK = {"ok": True, "result": {"message_id": 7}}


class HealChainTest(unittest.TestCase):
    def setUp(self):
        self._orig_api = ron_daemon.api
        # NOTE: ron_daemon's `rich_mod` is the module object IT imported
        # (its sys.path tweak resolves the live dir), not the repo copy
        # loaded above — patch the daemon's reference directly.
        self._rich_mod = ron_daemon.rich_mod
        self._orig_json = self._rich_mod.api_json

    def tearDown(self):
        ron_daemon.api = self._orig_api
        self._rich_mod.api_json = self._orig_json

    def _patch(self, results, json_results=None):
        mock = ScriptedAPI(results)
        ron_daemon.api = mock
        jmock = ScriptedAPI(json_results if json_results is not None
                            else results)
        self._rich_mod.api_json = jmock
        return mock, jmock

    # -- the core chain ----------------------------------------------------

    def test_parse_error_strips_formatting_and_retries(self):
        mock, _ = self._patch([
            _fail("HTTP 400: Bad Request: can't parse entities: "
                  "unsupported start tag"),
            _OK,
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            r = ron_daemon.send_healed(
                "sendMessage",
                {"chat_id": 1, "text": "*bold _broken",
                 "parse_mode": "Markdown"})
        self.assertTrue(r.get("ok"))
        self.assertEqual(len(mock.calls), 2)
        method2, params2 = mock.calls[1]
        self.assertEqual(method2, "sendMessage")
        self.assertNotIn("parse_mode", params2)  # formatting stripped
        self.assertEqual(params2["text"],
                         ron_daemon.strip_markdown("*bold _broken"))

    def test_too_long_truncates_to_4096(self):
        mock, _ = self._patch([
            _fail("HTTP 400: Bad Request: message is too long"),
            _OK,
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            r = ron_daemon.send_healed(
                "sendMessage", {"chat_id": 1, "text": "x" * 5000})
        self.assertTrue(r.get("ok"))
        self.assertEqual(len(mock.calls), 2)
        text2 = mock.calls[1][1]["text"]
        self.assertEqual(len(text2), 4096)
        self.assertTrue(text2.endswith("…"))

    def test_persistent_failure_gives_up_after_3_attempts(self):
        mock, _ = self._patch([
            _fail("HTTP 400: Bad Request: can't parse entities: bad"),
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            r = ron_daemon.send_healed(
                "sendMessage",
                {"chat_id": 1, "text": "*broken", "parse_mode": "Markdown"})
        self.assertFalse(r.get("ok"))  # caller surfaces the neutral error
        self.assertLessEqual(len(mock.calls), 3)
        self.assertEqual(len(mock.calls), 3)

    def test_unknown_error_fails_fast_no_retry(self):
        mock, _ = self._patch([_fail("HTTP 400: Bad Request: chat not found")])
        with contextlib.redirect_stdout(io.StringIO()):
            r = ron_daemon.send_healed(
                "sendMessage", {"chat_id": 1, "text": "hi"})
        self.assertFalse(r.get("ok"))
        self.assertEqual(len(mock.calls), 1)  # no guessing on unknowns

    def test_parse_then_length_uses_both_fixes(self):
        # Malformed AND too long: strip first (parse error), then truncate.
        mock, _ = self._patch([
            _fail("HTTP 400: Bad Request: can't parse entities: bad"),
            _fail("HTTP 400: Bad Request: message is too long"),
            _OK,
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            r = ron_daemon.send_healed(
                "sendMessage",
                {"chat_id": 1, "text": "*" + "x" * 5000,
                 "parse_mode": "Markdown"})
        self.assertTrue(r.get("ok"))
        self.assertEqual(len(mock.calls), 3)
        self.assertEqual(len(mock.calls[2][1]["text"]), 4096)

    def test_strip_noop_skips_wasted_attempt(self):
        # Parse error on already-plain short text: strip and truncate are
        # both no-ops, so no attempt is burned — fail after the original.
        mock, _ = self._patch([
            _fail("HTTP 400: Bad Request: can't parse entities: bad"),
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            r = ron_daemon.send_healed(
                "sendMessage", {"chat_id": 1, "text": "plain short"})
        self.assertFalse(r.get("ok"))
        self.assertEqual(len(mock.calls), 1)

    # -- edit path ----------------------------------------------------------

    def test_edit_path_heals_parse_error(self):
        mock, _ = self._patch([
            _fail("HTTP 400: Bad Request: can't parse entities: bad"),
            _OK,
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            r = ron_daemon.send_healed(
                "editMessageText",
                {"chat_id": 1, "message_id": 9, "text": "*broken",
                 "parse_mode": "Markdown"})
        self.assertTrue(r.get("ok"))
        self.assertEqual(len(mock.calls), 2)
        self.assertEqual(mock.calls[1][0], "editMessageText")
        self.assertNotIn("parse_mode", mock.calls[1][1])

    # -- rich path -----------------------------------------------------------

    def test_rich_blocks_dropped_to_plain_text(self):
        blocks = [{"type": "paragraph",
                   "text": [{"type": "bold", "text": "Hello"}]},
                  {"type": "divider"}]
        _, jmock = self._patch([], json_results=[
            _fail("HTTP 400: {\"ok\":false,\"error_code\":400,\"description\":"
                  "\"Bad Request: can't parse entities\"}"),
            _OK,
        ])
        payload = {"chat_id": 1, "rich_message": {"blocks": blocks},
                   "reply_parameters": {"message_id": 3}}
        with contextlib.redirect_stdout(io.StringIO()):
            r = ron_daemon.send_healed("sendRichMessage", payload,
                                       as_json=True)
        self.assertTrue(r.get("ok"))
        self.assertEqual(len(jmock.calls), 2)
        method2, params2 = jmock.calls[1]
        self.assertEqual(method2, "sendMessage")
        self.assertNotIn("rich_message", params2)
        self.assertIsInstance(params2["text"], str)
        self.assertIn("Hello", params2["text"])
        # Nested reply_parameters survived (JSON transport kept).
        self.assertEqual(params2["reply_parameters"], {"message_id": 3})

    def test_rich_markdown_shape_falls_back(self):
        _, jmock = self._patch([], json_results=[
            _fail("Bad Request: RICH_MESSAGE_CONTENT_REQUIRED"),
            _OK,
        ])
        payload = {"chat_id": 1,
                   "rich_message": {"markdown": "*broken _md"}}
        with contextlib.redirect_stdout(io.StringIO()):
            r = ron_daemon.send_healed("sendRichMessage", payload,
                                       as_json=True)
        self.assertTrue(r.get("ok"))
        params2 = jmock.calls[1][1]
        self.assertEqual(params2["text"],
                         ron_daemon.strip_markdown("*broken _md"))

    # -- wiring: public send() goes through the healer -----------------------

    def test_send_wires_through_healer(self):
        mock, _ = self._patch([
            _fail("HTTP 400: Bad Request: can't parse entities: bad"),
            _OK,
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            r = ron_daemon.send(1, "*broken", )
        self.assertTrue(r.get("ok"))
        self.assertEqual(len(mock.calls), 2)

    def test_send_markup_wires_through_healer(self):
        mock, _ = self._patch([
            _fail("HTTP 400: Bad Request: message is too long"),
            _OK,
        ])
        kb = {"inline_keyboard": [[{"text": "x", "callback_data": "y"}]]}
        with contextlib.redirect_stdout(io.StringIO()):
            r = ron_daemon.send_markup(1, "z" * 5000, kb)
        self.assertTrue(r.get("ok"))
        self.assertEqual(len(mock.calls), 2)
        # Keyboard kept through the heal — only the text changed.
        # (send_markup pre-serializes it to a JSON string.)
        import json as _json
        self.assertEqual(mock.calls[1][1]["reply_markup"], _json.dumps(kb))

    # -- observability ---------------------------------------------------------

    def test_heal_is_logged(self):
        self._patch([
            _fail("HTTP 400: Bad Request: can't parse entities: bad"),
            _OK,
        ])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ron_daemon.send_healed(
                "sendMessage",
                {"chat_id": 1, "text": "*broken", "parse_mode": "Markdown"})
        self.assertIn("[ron_daemon] send healed:", buf.getvalue())
        self.assertIn("can't parse entities", buf.getvalue())

    def test_success_is_silent(self):
        self._patch([_OK])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ron_daemon.send_healed("sendMessage",
                                   {"chat_id": 1, "text": "fine"})
        self.assertNotIn("send healed", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
