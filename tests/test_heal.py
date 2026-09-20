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


ron_daemon = None  # replaced below by the bootstrap


def _bootstrap_repo_modules():
    """Register the REPO copies of the daemon and its dependencies in
    sys.modules BEFORE the daemon itself is loaded.

    ron_daemon.py puts the live deploy dir first on sys.path (its
    line-27 tweak), so whichever test module imports the daemon first
    would otherwise cache the DEPLOYED ron_owner/ron_richblocks/... in
    sys.modules and poison every test file that runs later in the same
    process (python -m unittest discover). Registering the repo copies
    first makes the daemon's own `import ron_owner` etc. hit the cache,
    so the whole suite exercises repo code, not deployed code.
    """
    for _dep in ("http_pool", "ron_ai", "rich_send", "ron_cache",
                 "ron_health", "ron_richblocks", "ron_richvalidate",
                 "ron_vision", "ron_imagen", "ron_privacy", "ron_owner"):
        _load_repo_module(_dep)
    return _load_repo_module("ron_daemon")


ron_daemon = _bootstrap_repo_modules()


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


class _PatchedTransports(unittest.TestCase):
    """Same transport fixture as HealChainTest, without inheriting its
    test methods (avoids double-running them in subclasses)."""

    def setUp(self):
        self._orig_api = ron_daemon.api
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


class RichKeyboardHealTest(_PatchedTransports):
    """v1.15.0: every RICH_MESSAGE_* failure heals as a parse error; a
    keyboard failure (BUTTON_DATA_INVALID, bad button rows) drops the
    keyboard and retries text-only — stripping formatting can never fix
    a bad keyboard (audit D.7)."""

    def test_rich_message_errors_classify_as_parse(self):
        for err in ("Bad Request: RICH_MESSAGE_CONTENT_REQUIRED",
                    "Bad Request: RICH_MESSAGE_BUTTONS_TOO_MANY",
                    "Bad Request: RICH_MESSAGE_INVALID"):
            self.assertEqual(ron_daemon._heal_kind(err), "parse", err)

    def test_button_errors_classify_as_keyboard(self):
        for err in ("Bad Request: BUTTON_DATA_INVALID",
                    "Bad Request: button row is empty"):
            self.assertEqual(ron_daemon._heal_kind(err), "keyboard", err)

    def test_keyboard_error_drops_keyboard_retries_text_only(self):
        mock, _ = self._patch([
            _fail("HTTP 400: Bad Request: BUTTON_DATA_INVALID"),
            _OK,
        ])
        kb = {"inline_keyboard":
              [[{"text": "Tap", "callback_data": "q:Tap"}]]}
        with contextlib.redirect_stdout(io.StringIO()):
            r = ron_daemon.send_healed(
                "sendMessage",
                {"chat_id": 1, "text": "*hi*", "parse_mode": "Markdown",
                 "reply_markup": kb})
        self.assertTrue(r.get("ok"))
        self.assertEqual(len(mock.calls), 2)
        method2, params2 = mock.calls[1]
        self.assertEqual(method2, "sendMessage")
        self.assertNotIn("reply_markup", params2)  # keyboard dropped
        self.assertEqual(params2["text"], "*hi*")  # text/formatting kept
        self.assertEqual(params2["parse_mode"], "Markdown")

    def test_buttons_too_many_drops_rich_to_plain(self):
        _, jmock = self._patch([], json_results=[
            _fail("Bad Request: RICH_MESSAGE_BUTTONS_TOO_MANY"),
            _OK,
        ])
        blocks = [{"type": "paragraph", "text": "hello"}]
        with contextlib.redirect_stdout(io.StringIO()):
            r = ron_daemon.send_blocks(1, blocks)
        self.assertTrue(r.get("ok"))
        self.assertEqual(len(jmock.calls), 2)
        self.assertEqual(jmock.calls[0][0], "sendRichMessage")
        method2, params2 = jmock.calls[1]
        self.assertEqual(method2, "sendMessage")
        self.assertNotIn("rich_message", params2)
        self.assertEqual(params2["text"], "hello")

    def test_classic_keyboard_carries_no_style(self):
        # Blocks path fails -> classic markdown + under-bubble keyboard.
        # InlineKeyboardButton has no "style" field (it belongs to
        # in-bubble RichMessageButton only) — the fallback must not add it.
        import json as _json
        mock, jmock = self._patch([_OK],
                                  json_results=[_fail("Bad Request: "
                                                      "RICH_MESSAGE_CONTENT_REQUIRED")])
        with contextlib.redirect_stdout(io.StringIO()):
            ron_daemon.send_rich_with_kb(1, "Pick one\n[buttons: A | B]")
        kbs = []
        for m, p in (mock.calls + jmock.calls):
            rm = p.get("reply_markup")
            if rm is None:
                continue
            kbs.append(_json.loads(rm) if isinstance(rm, str) else rm)
        self.assertTrue(kbs, "expected a classic keyboard fallback send")
        for kb in kbs:
            for row in kb["inline_keyboard"]:
                for b in row:
                    self.assertNotIn("style", b)


class RichValidatorWiringTest(_PatchedTransports):
    """v1.15.0: an invalid rich payload logs LOUDLY, never touches the
    rich transport, and the user still gets a plain-text fallback."""

    def test_invalid_blocks_logged_loudly_and_transport_skipped(self):
        bad = [{"type": "table", "rows": [[{"text": "x"}]]}]  # wrong field
        sent = []
        orig_send = ron_daemon.send
        ron_daemon.send = lambda *a, **k: sent.append(
            a[1] if len(a) > 1 else "") or {"ok": True}
        try:
            _, jmock = self._patch([_OK])
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ron_daemon.send_blocks(1, bad)
            log = buf.getvalue()
        finally:
            ron_daemon.send = orig_send
        self.assertEqual(jmock.calls, [],
                         "rich transport must not be called")
        self.assertIn("RICH VALIDATION FAILED", log)
        self.assertIn("table", log)
        self.assertTrue(sent, "user still gets a plain-text fallback")

    def test_markdown_plus_blocks_rejected(self):
        payload = {"chat_id": 1,
                   "rich_message": {"markdown": "x", "blocks": []}}
        with contextlib.redirect_stdout(io.StringIO()):
            v = ron_daemon._rich_violations(payload, "sendRichMessage")
        self.assertTrue(v)
        self.assertTrue(any("exactly one of" in x for x in v))

    def test_warnings_only_do_not_trigger_fallback(self):
        inner = {"type": "bold", "text": "x"}
        for _ in range(20):  # nesting deeper than the validator ceiling
            inner = {"type": "bold", "text": [inner]}
        payload = {"chat_id": 1, "rich_message": {"blocks": [
            {"type": "paragraph", "text": inner}]}}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            v = ron_daemon._rich_violations(payload, "sendRichMessage")
        self.assertEqual(v, [])
        self.assertNotIn("RICH VALIDATION FAILED", buf.getvalue())

    def test_valid_blocks_pass_through_silently(self):
        _, jmock = self._patch([_OK])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = ron_daemon.send_blocks(
                1, [{"type": "paragraph", "text": "hi"}])
        self.assertTrue(r["ok"])
        self.assertNotIn("RICH VALIDATION FAILED", buf.getvalue())
        self.assertEqual(jmock.calls[0][0], "sendRichMessage")


class BlocksTextTest(unittest.TestCase):
    """_blocks_text handles the live-verified shapes (v1.15.0):
    blockquote.blocks, list-item blocks, table cells."""

    def test_new_block_shapes(self):
        blocks = [
            {"type": "blockquote", "blocks": [
                {"type": "paragraph", "text": "quoted"}]},
            {"type": "list", "items": [
                {"blocks": [{"type": "paragraph", "text": "one"}]},
                {"blocks": [{"type": "paragraph", "text": "two"}]}]},
            {"type": "table", "cells": [
                [{"text": "a"}, {"text": "b"}],
                [{"text": "c"}, {"text": "d"}]]},
        ]
        txt = ron_daemon._blocks_text(blocks)
        self.assertIn("quoted", txt)
        self.assertIn("\u2022 one", txt)
        self.assertIn("\u2022 two", txt)
        self.assertIn("a | b", txt)
        self.assertIn("c | d", txt)

    def test_old_shapes_yield_nothing(self):
        # The old (wrong) shapes must not leak stale text through the
        # fallback — blockquote.text / list-item.text / table.rows are
        # simply ignored.
        blocks = [
            {"type": "blockquote", "text": "stale"},
            {"type": "list", "items": [{"text": "stale"}]},
            {"type": "table", "rows": [[{"text": "stale"}]]},
        ]
        self.assertEqual(ron_daemon._blocks_text(blocks), "")


class StripMarkdownTest(unittest.TestCase):
    """v1.15.0: the no-format fallback strips spoiler/highlight markers
    and fences with non-word language tags (c++, c#)."""

    def test_spoiler_and_highlight_removed(self):
        self.assertEqual(ron_daemon.strip_markdown("a ||spoil|| b ==hl== c"),
                         "a spoil b hl c")

    def test_fence_with_nonword_language(self):
        self.assertEqual(ron_daemon.strip_markdown("```c++\nint x;\n```"),
                         "int x;")


class TrailerFixesTest(unittest.TestCase):
    """v1.15.0 trailer hardening: lone markers cleaned from labels,
    non-http link targets keep the label visible and are logged."""

    def test_lone_markers_stripped_from_labels(self):
        clean, kb = ron_daemon.parse_trailers(
            "Star score update: you are not alone\n"
            "[buttons: *star* alone | _under_ score]")
        self.assertEqual([b["text"] for b in kb["inline_keyboard"][0]],
                         ["star alone", "under score"])

    def test_nonhttp_link_label_stays_visible_and_logged(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            clean, kb = ron_daemon.parse_trailers(
                "Src\n[links: my page -> ftp://example.com/x]")
        self.assertIn("my page", clean)  # label kept as visible text
        self.assertIsNone(kb)  # no link button built
        self.assertIn("non-http", buf.getvalue())  # logged loudly

    def test_nonhttp_arrow_in_buttons_kept_as_label(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            clean, kb = ron_daemon.parse_trailers(
                "Read the docs first\n[buttons: docs -> not-a-url]")
        self.assertEqual(kb["inline_keyboard"][0][0]["text"], "docs")
        self.assertIn("non-http", buf.getvalue())
