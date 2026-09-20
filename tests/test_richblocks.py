#!/usr/bin/env python3
"""Offline tests for the Telegram Markdown -> rich blocks converter.

No network. Run: python3 -m unittest discover tests -v
(from ~/workspace/telegram-bot)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ron_richblocks import parse_inline, text_blocks, markdown_to_blocks


class TestParseInline(unittest.TestCase):
    def test_plain_returns_str(self):
        self.assertEqual(parse_inline("hello world"), "hello world")
        self.assertEqual(parse_inline(""), "")

    def test_bold(self):
        self.assertEqual(parse_inline("a **bold** b"),
                         ["a ", {"type": "bold", "text": "bold"}, " b"])

    def test_italic_star(self):
        self.assertEqual(parse_inline("a *it* b"),
                         ["a ", {"type": "italic", "text": "it"}, " b"])

    def test_italic_underscore(self):
        self.assertEqual(parse_inline("a _it_ b"),
                         ["a ", {"type": "italic", "text": "it"}, " b"])

    def test_underscore_in_word_not_italic(self):
        self.assertEqual(parse_inline("like_this_here"), "like_this_here")

    def test_code_literal(self):
        out = parse_inline("run `a **b**` now")
        self.assertIn({"type": "code", "text": "a **b**"}, out)

    def test_strike_spoiler_marked_underline(self):
        self.assertEqual(parse_inline("~~s~~")["type"], "strikethrough")
        self.assertEqual(parse_inline("||x||")["type"], "spoiler")
        self.assertEqual(parse_inline("==h==")["type"], "marked")
        self.assertEqual(parse_inline("__u__")["type"], "underline")

    def test_link(self):
        out = parse_inline("[Tap](https://example.com/x)")
        self.assertEqual(out, {"type": "url", "text": "Tap",
                               "url": "https://example.com/x"})

    def test_link_with_bold_text(self):
        out = parse_inline("[**Tap**](https://example.com)")
        self.assertEqual(out["type"], "url")
        self.assertEqual(out["text"], {"type": "bold", "text": "Tap"})

    def test_unbalanced_left_literal(self):
        self.assertEqual(parse_inline("a **oops b"), "a **oops b")
        self.assertEqual(parse_inline("a ||x b"), "a ||x b")

    def test_escape(self):
        self.assertEqual(parse_inline(r"a \* b"), "a * b")

    def test_adjacent_merge(self):
        self.assertEqual(parse_inline("**a** and **b**"),
                         [{"type": "bold", "text": "a"}, " and ",
                          {"type": "bold", "text": "b"}])


class TestTextBlocks(unittest.TestCase):
    def test_splits_on_blank_lines(self):
        blks = text_blocks("one\n\ntwo")
        self.assertEqual(len(blks), 2)
        self.assertTrue(all(b["type"] == "paragraph" for b in blks))

    def test_empty_safe(self):
        self.assertEqual(text_blocks("")[0]["type"], "paragraph")


class TestMarkdownToBlocks(unittest.TestCase):
    def test_paragraphs(self):
        blks = markdown_to_blocks("hello\n\nworld")
        self.assertEqual([b["type"] for b in blks],
                         ["paragraph", "paragraph"])

    def test_heading_degrades_to_bold(self):
        blks = markdown_to_blocks("## Title")
        self.assertEqual(blks[0]["type"], "paragraph")
        self.assertEqual(blks[0]["text"][0]["type"], "bold")

    def test_quote(self):
        blks = markdown_to_blocks("> quoted\n> lines")
        self.assertEqual(blks[0]["type"], "blockquote")

    def test_bullet_list(self):
        blks = markdown_to_blocks("- a\n- b")
        self.assertEqual(blks[0]["type"], "list")
        self.assertFalse(blks[0]["ordered"])
        self.assertEqual(len(blks[0]["items"]), 2)

    def test_ordered_list(self):
        blks = markdown_to_blocks("1. a\n2. b")
        self.assertTrue(blks[0]["ordered"])

    def test_code_fence(self):
        blks = markdown_to_blocks("```python\nx = 1\n```")
        self.assertEqual(blks[0]["type"], "pre")
        self.assertEqual(blks[0]["language"], "python")
        self.assertIn("x = 1", blks[0]["text"])

    def test_divider(self):
        blks = markdown_to_blocks("a\n\n---\n\nb")
        self.assertEqual([b["type"] for b in blks],
                         ["paragraph", "divider", "paragraph"])

    def test_table_degrades_to_pre(self):
        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        blks = markdown_to_blocks(md)
        self.assertEqual(blks[0]["type"], "pre")
        self.assertIn("A", blks[0]["text"])
        self.assertIn("1", blks[0]["text"])

    def test_spoiler_line_not_table(self):
        # "||x||" starts+ends with "|" but is a spoiler, not a table.
        blks = markdown_to_blocks("the treasure was ||under the oak||")
        self.assertEqual(blks[0]["type"], "paragraph")

    def test_inline_formatting_kept(self):
        blks = markdown_to_blocks("a **bold** and [link](https://x.y)")
        text = blks[0]["text"]
        types = [p["type"] for p in text if isinstance(p, dict)]
        self.assertIn("bold", types)
        self.assertIn("url", types)

    def test_never_empty(self):
        self.assertTrue(markdown_to_blocks(""))
        self.assertTrue(markdown_to_blocks("\n\n"))


if __name__ == "__main__":
    unittest.main()
