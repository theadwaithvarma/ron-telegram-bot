#!/usr/bin/env python3
"""v1.18.0 tests: native rich-formatting blocks (Bot API 10.1-10.3).

Covers the block converter additions in ron_richblocks: compact tables
with header cells, checklists, fenced code with language, <details>
expandable sections (incl. the "Tap to expand" shorthand), long-quote
collapse, headings, needs_blocks_form routing, and malformed-input
fallbacks. Also covers the in-body button row builder (via ron_daemon)
including the 64-byte callback_data cap, and validator acceptance of the
new shapes.

No network. Run: python3 tests/test_rich_blocks.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ron_richblocks import markdown_to_blocks, needs_blocks_form
import ron_richvalidate


class TestTable(unittest.TestCase):
    def test_compact_and_header_cells(self):
        md = "| Item | Price |\n|---|---|\n| Apple | 120 |\n| Mango | 80 |"
        blks = markdown_to_blocks(md)
        self.assertEqual(len(blks), 1)
        t = blks[0]
        self.assertEqual(t["type"], "table")
        self.assertTrue(t["is_compact"])
        header = t["cells"][0]
        self.assertTrue(all(c.get("is_header") for c in header))
        self.assertEqual([c["text"] for c in header], ["Item", "Price"])
        # data rows are NOT header cells
        self.assertFalse(any("is_header" in c for c in t["cells"][1]))
        self.assertEqual([c["text"] for c in t["cells"][1]],
                         ["Apple", "120"])

    def test_needs_blocks_form(self):
        blks = markdown_to_blocks("| A | B |\n|---|---|\n| 1 | 2 |")
        self.assertTrue(needs_blocks_form(blks))
        self.assertFalse(needs_blocks_form(
            markdown_to_blocks("just **bold** text")))

    def test_validator_accepts_table(self):
        blks = markdown_to_blocks("| A | B |\n|---|---|\n| 1 | 2 |")
        payload = {"chat_id": 1, "rich_message": {"blocks": blks}}
        self.assertEqual(
            ron_richvalidate.validate_outgoing(payload, "sendRichMessage"),
            [])


class TestChecklist(unittest.TestCase):
    def test_checkbox_items(self):
        blks = markdown_to_blocks("- [ ] Buy milk\n- [x] Buy eggs")
        self.assertEqual(len(blks), 1)
        lst = blks[0]
        self.assertEqual(lst["type"], "list")
        self.assertFalse(lst["ordered"])
        self.assertEqual(len(lst["items"]), 2)
        self.assertTrue(lst["items"][0]["has_checkbox"])
        self.assertFalse(lst["items"][0]["is_checked"])
        self.assertTrue(lst["items"][1]["is_checked"])
        self.assertIn("Buy milk",
                      str(lst["items"][0]["blocks"][0]["text"]))
        self.assertIn("Buy eggs",
                      str(lst["items"][1]["blocks"][0]["text"]))

    def test_checklist_not_plain_bullet(self):
        # "- [ ] task" must not parse as a bullet with literal "[ ] task".
        blks = markdown_to_blocks("- [ ] task")
        self.assertIn("has_checkbox", blks[0]["items"][0])

    def test_checklist_separate_from_bullets(self):
        blks = markdown_to_blocks("- plain\n- [ ] checked-task")
        self.assertEqual(len(blks), 2)
        self.assertNotIn("has_checkbox", blks[0]["items"][0])
        self.assertIn("has_checkbox", blks[1]["items"][0])

    def test_needs_blocks_form(self):
        self.assertTrue(needs_blocks_form(
            markdown_to_blocks("- [ ] a")))
        self.assertFalse(needs_blocks_form(
            markdown_to_blocks("- a\n- b")))

    def test_validator_accepts_checklist(self):
        blks = markdown_to_blocks("- [ ] a\n- [X] b")
        payload = {"chat_id": 1, "rich_message": {"blocks": blks}}
        self.assertEqual(
            ron_richvalidate.validate_outgoing(payload, "sendRichMessage"),
            [])

    def test_validator_rejects_bad_checkbox_flag(self):
        bad = {"type": "list", "ordered": False, "items": [
            {"blocks": [{"type": "paragraph", "text": "x"}],
             "has_checkbox": "yes"}]}
        payload = {"chat_id": 1, "rich_message": {"blocks": [bad]}}
        viols = ron_richvalidate.validate_outgoing(payload,
                                                   "sendRichMessage")
        self.assertTrue(any("has_checkbox" in v for v in viols))


class TestPre(unittest.TestCase):
    def test_language_kept(self):
        blks = markdown_to_blocks("```python\ndef hello():\n    return 'world'\n```")
        self.assertEqual(blks[0]["type"], "pre")
        self.assertEqual(blks[0]["language"], "python")
        self.assertIn("def hello():", blks[0]["text"])

    def test_no_language(self):
        blks = markdown_to_blocks("```\nplain\n```")
        self.assertEqual(blks[0]["type"], "pre")
        self.assertNotIn("language", blks[0])


class TestDetails(unittest.TestCase):
    def test_details_tags(self):
        md = ("<details><summary>Tap to expand details</summary>\n"
              "Hidden line one.\nHidden line two.\n</details>")
        blks = markdown_to_blocks(md)
        self.assertEqual(len(blks), 1)
        d = blks[0]
        self.assertEqual(d["type"], "details")
        self.assertEqual(d["summary"], "Tap to expand details")
        self.assertNotIn("is_open", d)  # collapsed by default
        self.assertTrue(any("Hidden line one" in str(b.get("text", ""))
                            for b in d["blocks"]))

    def test_details_multiline_open_close(self):
        md = ("Some intro\n\n<details>\n<summary>More</summary>\n"
              "body *here*\n</details>\n\nafter")
        blks = markdown_to_blocks(md)
        types = [b["type"] for b in blks]
        self.assertEqual(types, ["paragraph", "details", "paragraph"])
        d = blks[1]
        self.assertEqual(d["summary"], "More")
        self.assertEqual(d["blocks"][0]["type"], "paragraph")

    def test_expand_marker_shorthand(self):
        md = "Tap to expand: release notes\nline one\nline two\n\nnext para"
        blks = markdown_to_blocks(md)
        self.assertEqual(blks[0]["type"], "details")
        self.assertEqual(blks[0]["summary"], "release notes")
        self.assertEqual(blks[1]["type"], "paragraph")

    def test_marker_not_mid_paragraph(self):
        # "tap to expand" inside a sentence is not a block.
        blks = markdown_to_blocks("click here and tap to expand the menu")
        self.assertEqual(blks[0]["type"], "paragraph")

    def test_unclosed_details_falls_back(self):
        blks = markdown_to_blocks("<details><summary>oops\nno closing tag")
        self.assertTrue(all(b["type"] == "paragraph" for b in blks))

    def test_needs_blocks_form(self):
        self.assertTrue(needs_blocks_form(
            markdown_to_blocks("<details><summary>x</summary>y</details>")))

    def test_validator_accepts_details(self):
        blks = markdown_to_blocks(
            "<details><summary>x</summary>body</details>")
        payload = {"chat_id": 1, "rich_message": {"blocks": blks}}
        self.assertEqual(
            ron_richvalidate.validate_outgoing(payload, "sendRichMessage"),
            [])


class TestQuoteAndHeading(unittest.TestCase):
    def test_short_quote_stays_blockquote(self):
        blks = markdown_to_blocks("> short quote")
        self.assertEqual(blks[0]["type"], "blockquote")

    def test_long_quote_collapses(self):
        long_q = "> " + "word " * 120
        blks = markdown_to_blocks(long_q.strip())
        self.assertEqual(blks[0]["type"], "expandable_blockquote")
        self.assertIn("word", str(blks[0]["text"]))

    def test_heading_sizes(self):
        for n in range(1, 7):
            blks = markdown_to_blocks("#" * n + " Head")
            self.assertEqual(blks[0]["type"], "heading")
            self.assertEqual(blks[0]["size"], n)

    def test_heading_needs_blocks_form(self):
        self.assertTrue(needs_blocks_form(
            markdown_to_blocks("## Section")))

    def test_validator_accepts_new_shapes(self):
        long_q = "> " + "word " * 120
        blks = markdown_to_blocks("## H\n" + long_q.strip())
        payload = {"chat_id": 1, "rich_message": {"blocks": blks}}
        self.assertEqual(
            ron_richvalidate.validate_outgoing(payload, "sendRichMessage"),
            [])


class TestFallbacks(unittest.TestCase):
    def test_garbage_never_raises(self):
        for md in ["", "   ", "```", "```\n```", "|", "||||", ">",
                   "<details>", "- [ ]", "##", "[bad](notaurl)",
                   "**unclosed", "| a |\nnot a table row"]:
            blks = markdown_to_blocks(md)  # must not raise
            self.assertTrue(blks)
            for b in blks:
                payload = {"chat_id": 1,
                           "rich_message": {"blocks": [b]}}
                viols = ron_richvalidate.validate_outgoing(
                    payload, "sendRichMessage")
                hard = [v for v in viols if not v.startswith("WARN")]
                self.assertEqual(hard, [], f"{md!r}: {hard}")

    def test_empty_fence_dropped(self):
        blks = markdown_to_blocks("```\n```")
        self.assertFalse(any(b["type"] == "pre" for b in blks))


class TestInBodyButtons(unittest.TestCase):
    """The buttons block builder lives in ron_daemon; these tests pin the
    v1.18.0 contract (1-8 per row, 64-byte callback cap, align)."""

    @classmethod
    def setUpClass(cls):
        import ron_daemon
        cls.rd = ron_daemon

    def test_row_shape(self):
        blk = self.rd.rich_buttons_block(
            [{"text": "Visit", "url": "https://example.com"},
             {"text": "OK", "callback_data": "q:ok"}])
        self.assertEqual(blk["type"], "buttons")
        self.assertEqual(blk["align"], "left")
        self.assertEqual(len(blk["buttons"]), 2)
        self.assertEqual(blk["buttons"][1]["callback_data"], "q:ok")

    def test_callback_64_byte_cap(self):
        long_cb = "q:" + "é" * 100  # multi-byte: bytes, not chars, count
        blk = self.rd.rich_buttons_block(
            [{"text": "x", "callback_data": long_cb}])
        n = len(blk["buttons"][0]["callback_data"].encode("utf-8"))
        self.assertLessEqual(n, 64)
        self.assertGreater(n, 0)

    def test_row_size_limits(self):
        with self.assertRaises(ValueError):
            self.rd.rich_buttons_block([])
        with self.assertRaises(ValueError):
            self.rd.rich_buttons_block(
                [{"text": str(i), "callback_data": str(i)}
                 for i in range(9)])

    def test_align(self):
        blk = self.rd.rich_buttons_block(
            [{"text": "x", "callback_data": "y"}], align="center")
        self.assertEqual(blk["align"], "center")
        blk = self.rd.rich_buttons_block(
            [{"text": "x", "callback_data": "y"}], align="bogus")
        self.assertEqual(blk["align"], "left")

    def test_validator_accepts_buttons_block(self):
        blk = self.rd.rich_buttons_block(
            [{"text": "OK", "callback_data": "q:ok", "style": "success"}])
        payload = {"chat_id": 1, "rich_message": {"blocks": [blk]}}
        self.assertEqual(
            ron_richvalidate.validate_outgoing(payload, "sendRichMessage"),
            [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
