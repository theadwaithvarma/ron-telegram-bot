#!/usr/bin/env python3
"""Offline tests for Bot API 10.3 rich message buttons (no network).

Run: python3 tests/test_rich_buttons.py  (or python3 -m unittest discover tests)

Covers: the InputRichBlockButtons builder (shape, exactly-one-action rule,
1-8 buttons per row, 64-byte callback cap, style passthrough/defaults),
the plain text block helper, and the privacy approval button rows used by
both the rich path and the classic keyboard fallback.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_daemon
import ron_privacy


def _row(*btns):
    return [list(btns)]


class TestRichButtonsBlock(unittest.TestCase):
    def test_shape(self):
        blk = ron_daemon.rich_buttons_block(
            _row({"text": "Yes", "callback_data": "mem:go:1",
                  "style": "success"})[0])
        self.assertEqual(blk["type"], "buttons")
        self.assertEqual(blk["align"], "left")
        self.assertEqual(len(blk["buttons"]), 1)
        b = blk["buttons"][0]
        self.assertEqual(b["text"], "Yes")
        self.assertEqual(b["callback_data"], "mem:go:1")
        self.assertEqual(b["style"], "success")

    def test_align_passthrough_and_invalid(self):
        row = _row({"text": "A", "callback_data": "x"})[0]
        self.assertEqual(ron_daemon.rich_buttons_block(row, "center")["align"],
                         "center")
        self.assertEqual(ron_daemon.rich_buttons_block(row, "nope")["align"],
                         "left")

    def test_default_style_callback_is_primary(self):
        blk = ron_daemon.rich_buttons_block(
            _row({"text": "A", "callback_data": "x"})[0])
        self.assertEqual(blk["buttons"][0]["style"], "primary")

    def test_url_button_no_forced_style(self):
        blk = ron_daemon.rich_buttons_block(
            _row({"text": "Site", "url": "https://example.com"})[0])
        b = blk["buttons"][0]
        self.assertEqual(b["url"], "https://example.com")
        self.assertNotIn("style", b)

    def test_exactly_one_action(self):
        with self.assertRaises(ValueError):
            ron_daemon.rich_buttons_block(
                _row({"text": "A"})[0])  # no action
        with self.assertRaises(ValueError):
            ron_daemon.rich_buttons_block(
                _row({"text": "A", "callback_data": "x",
                      "url": "https://example.com"})[0])  # two actions

    def test_row_size_limits(self):
        with self.assertRaises(ValueError):
            ron_daemon.rich_buttons_block([])  # empty row
        with self.assertRaises(ValueError):
            ron_daemon.rich_buttons_block(
                [{"text": str(i), "callback_data": str(i)}
                 for i in range(9)])  # 9 > 8
        ok = ron_daemon.rich_buttons_block(
            [{"text": str(i), "callback_data": str(i)} for i in range(8)])
        self.assertEqual(len(ok["buttons"]), 8)

    def test_callback_data_64_byte_cap(self):
        long_cb = "q:" + "é" * 100  # multibyte: bytes, not chars
        blk = ron_daemon.rich_buttons_block(
            _row({"text": "A", "callback_data": long_cb})[0])
        cb = blk["buttons"][0]["callback_data"]
        self.assertLessEqual(len(cb.encode("utf-8")), 64)
        cb.encode("utf-8")  # must not raise (no split codepoint)

    def test_empty_rows_rejected(self):
        with self.assertRaises(ValueError):
            ron_daemon.rich_buttons_blocks([])

    def test_one_block_per_row(self):
        # One row per buttons block (Telegram: 1-8 buttons per row-block);
        # rows are NOT flattened into a single block anymore.
        blks = ron_daemon.rich_buttons_blocks([
            [{"text": "A", "callback_data": "a"}],
            [{"text": "B", "url": "https://example.com"}],
        ])
        self.assertEqual(len(blks), 2)
        self.assertEqual([b["text"] for b in blks[0]["buttons"]], ["A"])
        self.assertEqual([b["text"] for b in blks[1]["buttons"]], ["B"])
        for blk in blks:
            self.assertEqual(blk["type"], "buttons")

    def test_more_action_types(self):
        # RichMessageButton takes the same actions as an inline button.
        blk = ron_daemon.rich_buttons_block([
            {"text": "App", "web_app": {"url": "https://example.com/app"}},
        ])
        self.assertEqual(blk["buttons"][0]["web_app"],
                         {"url": "https://example.com/app"})
        blk = ron_daemon.rich_buttons_block([
            {"text": "Copy", "copy_text": {"text": "code-123"}},
        ])
        self.assertEqual(blk["buttons"][0]["copy_text"], {"text": "code-123"})
        blk = ron_daemon.rich_buttons_block([
            {"text": "Search", "switch_inline_query": ""},
        ])
        self.assertIn("switch_inline_query", blk["buttons"][0])

    def test_exactly_one_action_across_all_types(self):
        with self.assertRaises(ValueError):
            ron_daemon.rich_buttons_block(
                [{"text": "A", "url": "https://example.com",
                  "web_app": {"url": "https://example.com"}}])


class TestRichTextBlock(unittest.TestCase):
    def test_shape(self):
        # The old {"type": "text", ...} shape was rejected by the API
        # ("type text is unsupported"); the text block is a paragraph.
        blk = ron_daemon.rich_text_block("hello")
        self.assertEqual(blk["type"], "paragraph")
        self.assertEqual(blk["text"], "hello")

    def test_empty_safe(self):
        self.assertEqual(ron_daemon.rich_text_block("")["text"], "")
        self.assertEqual(ron_daemon.rich_text_block(None)["text"], "")


class TestApprovalButtons(unittest.TestCase):
    def test_rows_carry_semantic_styles(self):
        rows = ron_privacy.approval_button_rows(4242)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 2)
        yes, no = rows[0]
        self.assertEqual(yes["style"], "success")
        self.assertEqual(no["style"], "danger")
        self.assertEqual(yes["callback_data"], "mem:go:4242")
        self.assertEqual(no["callback_data"], "mem:no:4242")

    def test_rich_block_from_rows(self):
        blks = ron_daemon.rich_buttons_blocks(
            ron_privacy.approval_button_rows(7))
        self.assertEqual(len(blks), 1)
        blk = blks[0]
        styles = [b["style"] for b in blk["buttons"]]
        self.assertEqual(styles, ["success", "danger"])
        datas = [b["callback_data"] for b in blk["buttons"]]
        self.assertEqual(datas, ["mem:go:7", "mem:no:7"])
        for d in datas:  # within the 64-byte cap
            self.assertLessEqual(len(d.encode("utf-8")), 64)

    def test_classic_keyboard_unchanged(self):
        # The fallback path must stay byte-identical: text + callback_data.
        kb = ron_privacy.approval_keyboard(99)
        row = kb["inline_keyboard"][0]
        self.assertEqual(
            [(b["text"], b["callback_data"]) for b in row],
            [("✅ Yes, share", "mem:go:99"),
             ("❌ Keep private", "mem:no:99")])
        for b in row:
            self.assertEqual(set(b.keys()), {"text", "callback_data"})


class TestTrailerLabelSanitizer(unittest.TestCase):
    """parse_trailers must never show raw Markdown/URLs in button labels."""

    def test_markdown_stripped_from_labels(self):
        clean, kb = ron_daemon.parse_trailers(
            "Pick one\n[buttons: Open **Muse** | `code` thing | ||spoiler|| x]")
        row = kb["inline_keyboard"][0]
        self.assertEqual([b["text"] for b in row],
                         ["Open Muse", "code thing", "spoiler x"])

    def test_link_syntax_inside_buttons_rescued(self):
        clean, kb = ron_daemon.parse_trailers(
            "News\n[buttons: 🔵 Read more -> https://example.com/a | Chat]")
        rows = kb["inline_keyboard"]
        self.assertEqual(rows[0][0]["text"], "Chat")  # quick-reply row
        self.assertEqual(rows[1][0]["text"], "🔵 Read more")  # URL row
        self.assertEqual(rows[1][0]["url"], "https://example.com/a")

    def test_links_trailer_labels_cleaned(self):
        clean, kb = ron_daemon.parse_trailers(
            "Src\n[links: **Bold** src -> https://example.com]")
        self.assertEqual(kb["inline_keyboard"][0][0]["text"], "Bold src")

    def test_empty_labels_dropped(self):
        clean, kb = ron_daemon.parse_trailers(
            "Hi\n[buttons:  |   | Real]")
        self.assertEqual([b["text"] for b in kb["inline_keyboard"][0]],
                         ["Real"])

    def test_callback_data_matches_visible_label(self):
        # Tapping re-sends the label; data must echo the CLEANED text.
        clean, kb = ron_daemon.parse_trailers(
            "Hi\n[buttons: **Hey** there]")
        b = kb["inline_keyboard"][0][0]
        self.assertEqual(b["text"], "Hey there")
        self.assertEqual(b["callback_data"], "q:Hey there")


if __name__ == "__main__":
    unittest.main(verbosity=1)
