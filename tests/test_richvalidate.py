#!/usr/bin/env python3
"""Offline tests for the strict Telegram outgoing-payload validator.

No network, stdlib only. Run: python3 -m unittest discover tests -v
(from ~/workspace/ron-telegram-bot)
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ron_richvalidate import validate_outgoing


def hard_errors(payload, method):
    """Violations minus advisory WARN: entries (deep-nesting warnings)."""
    return [e for e in validate_outgoing(payload, method)
            if not e.startswith("WARN: ")]


def assert_valid(tc, payload, method):
    errs = hard_errors(payload, method)
    tc.assertEqual(errs, [], f"expected valid {method} payload, got: {errs}")


def assert_invalid(tc, payload, method, needle=None):
    errs = hard_errors(payload, method)
    tc.assertTrue(errs, f"expected violations for {method}, got none")
    if needle is not None:
        tc.assertTrue(any(needle in e for e in errs),
                     f"expected {needle!r} among violations: {errs}")


def cb(text):
    return {"text": text, "callback_data": "d"}


def rich(blocks, **kw):
    p = {"chat_id": 1, "rich_message": {"blocks": blocks}}
    p.update(kw)
    return p


# ---------------------------------------------------------------------------
# Rule 1: sendRichMessage — exactly one of blocks/html/markdown
# ---------------------------------------------------------------------------

class TestExactlyOneMode(unittest.TestCase):
    def test_blocks_only_valid(self):
        assert_valid(self, rich([{"type": "paragraph", "text": "hi"}]),
                     "sendRichMessage")

    def test_markdown_only_valid(self):
        assert_valid(self, {"chat_id": 1, "rich_message": {"markdown": "**hi**"}},
                     "sendRichMessage")

    def test_html_only_valid(self):
        assert_valid(self, {"chat_id": 1, "rich_message": {"html": "<b>hi</b>"}},
                     "sendRichMessage")

    def test_blocks_and_markdown_rejected(self):
        p = {"chat_id": 1, "rich_message": {
            "blocks": [{"type": "paragraph", "text": "hi"}], "markdown": "**hi**"}}
        assert_invalid(self, p, "sendRichMessage", "exactly one")

    def test_blocks_and_html_rejected(self):
        p = {"chat_id": 1, "rich_message": {
            "blocks": [{"type": "paragraph", "text": "hi"}], "html": "<b>hi</b>"}}
        assert_invalid(self, p, "sendRichMessage", "exactly one")

    def test_markdown_and_html_rejected(self):
        p = {"chat_id": 1, "rich_message": {"markdown": "x", "html": "x"}}
        assert_invalid(self, p, "sendRichMessage", "exactly one")

    def test_all_three_rejected(self):
        p = {"chat_id": 1, "rich_message": {
            "blocks": [{"type": "paragraph", "text": "x"}],
            "markdown": "x", "html": "x"}}
        assert_invalid(self, p, "sendRichMessage", "exactly one")

    def test_none_set_rejected(self):
        assert_invalid(self, {"chat_id": 1, "rich_message": {"is_rtl": True}},
                       "sendRichMessage", "exactly one")

    def test_rich_message_missing_rejected(self):
        assert_invalid(self, {"chat_id": 1}, "sendRichMessage", "required dict")

    def test_empty_blocks_rejected(self):
        assert_invalid(self, rich([]), "sendRichMessage", "non-empty")

    def test_blocks_not_list_rejected(self):
        assert_invalid(self, rich({"type": "paragraph"}), "sendRichMessage",
                       "non-empty")

    def test_empty_markdown_rejected(self):
        assert_invalid(self, {"chat_id": 1, "rich_message": {"markdown": ""}},
                       "sendRichMessage", "non-empty string")

    def test_payload_not_dict_never_raises(self):
        errs = validate_outgoing("nope", "sendMessage")
        self.assertTrue(errs)


# ---------------------------------------------------------------------------
# Rule 2: blocks
# ---------------------------------------------------------------------------

class TestBlocks(unittest.TestCase):
    # paragraph / blockquote / footer
    def test_paragraph_valid(self):
        assert_valid(self, rich([{"type": "paragraph", "text": "hi"}]),
                     "sendRichMessage")

    def test_paragraph_missing_text_rejected(self):
        assert_invalid(self, rich([{"type": "paragraph"}]), "sendRichMessage",
                       "required RichText")

    def test_paragraph_bad_richtext_rejected(self):
        p = rich([{"type": "paragraph", "text": {"type": "text", "text": "x"}}])
        assert_invalid(self, p, "sendRichMessage", "not a real RichText node")

    def test_blockquote_valid(self):
        # Live-verified (2026-09-21): blockquote takes nested "blocks";
        # {"text": ...} 400s RICH_MESSAGE_EMPTY.
        b = {"type": "blockquote",
             "blocks": [{"type": "paragraph", "text": ["a", "\n", "b"]},
                        {"type": "divider"}]}
        assert_valid(self, rich([b]), "sendRichMessage")

    def test_blockquote_text_shape_rejected(self):
        b = {"type": "blockquote", "text": ["a", "\n", "b"]}
        assert_invalid(self, rich([b]), "sendRichMessage", "unexpected fields")

    def test_footer_valid(self):
        assert_valid(self, rich([{"type": "footer", "text": "fn"}]),
                     "sendRichMessage")

    # heading
    def test_heading_valid(self):
        assert_valid(self, rich([{"type": "heading", "text": "T", "size": 2}]),
                     "sendRichMessage")

    def test_heading_size_0_rejected(self):
        assert_invalid(self, rich([{"type": "heading", "text": "T", "size": 0}]),
                       "sendRichMessage", "int 1-6")

    def test_heading_size_7_rejected(self):
        assert_invalid(self, rich([{"type": "heading", "text": "T", "size": 7}]),
                       "sendRichMessage", "int 1-6")

    def test_heading_size_str_rejected(self):
        assert_invalid(self, rich([{"type": "heading", "text": "T", "size": "2"}]),
                       "sendRichMessage", "int 1-6")

    def test_heading_size_bool_rejected(self):
        assert_invalid(self, rich([{"type": "heading", "text": "T", "size": True}]),
                       "sendRichMessage", "int 1-6")

    # pre
    def test_pre_valid_no_language(self):
        assert_valid(self, rich([{"type": "pre", "text": "x = 1"}]),
                     "sendRichMessage")

    def test_pre_valid_with_language(self):
        assert_valid(self, rich([{"type": "pre", "text": "x = 1",
                                        "language": "python"}]),
                     "sendRichMessage")

    def test_pre_empty_text_rejected(self):
        assert_invalid(self, rich([{"type": "pre", "text": ""}]),
                       "sendRichMessage", "non-empty")

    def test_pre_missing_text_rejected(self):
        assert_invalid(self, rich([{"type": "pre"}]), "sendRichMessage",
                       "required RichText")

    def test_pre_language_non_str_rejected(self):
        assert_invalid(self, rich([{"type": "pre", "text": "x", "language": 3}]),
                       "sendRichMessage", "must be a string")

    # divider
    def test_divider_valid(self):
        assert_valid(self, rich([{"type": "divider"}]), "sendRichMessage")

    def test_divider_extra_field_rejected(self):
        assert_invalid(self, rich([{"type": "divider", "text": "x"}]),
                       "sendRichMessage", "no fields beyond")

    # list (live-verified: items are {"blocks": [...]}; {"text": ...} 400s)
    def test_list_valid(self):
        p = rich([{"type": "list", "ordered": False,
                   "items": [
                       {"blocks": [{"type": "paragraph", "text": "a"}]},
                       {"blocks": [{"type": "paragraph",
                                     "text": ["b", {"type": "bold",
                                                    "text": "c"}]}]}]}])
        assert_valid(self, p, "sendRichMessage")

    def test_list_ordered_missing_rejected(self):
        assert_invalid(self, rich([{"type": "list", "items": [{"text": "a"}]}]),
                       "sendRichMessage", "must be bool")

    def test_list_ordered_str_rejected(self):
        assert_invalid(self, rich([{"type": "list", "ordered": "yes",
                                         "items": [{"text": "a"}]}]),
                       "sendRichMessage", "must be bool")

    def test_list_items_empty_rejected(self):
        assert_invalid(self, rich([{"type": "list", "ordered": True, "items": []}]),
                       "sendRichMessage", "non-empty list")

    def test_list_item_text_shape_rejected(self):
        # The old guessed {"text": ...} item shape 400s on the server.
        assert_invalid(self, rich([{"type": "list", "ordered": True,
                                         "items": [{"text": "a"}]}]),
                       "sendRichMessage", "unexpected fields")

    def test_list_item_missing_blocks_rejected(self):
        assert_invalid(self, rich([{"type": "list", "ordered": True,
                                         "items": [{"blocks": None}]}]),
                       "sendRichMessage", "must be a non-empty list of blocks")

    def test_list_item_empty_blocks_rejected(self):
        assert_invalid(self, rich([{"type": "list", "ordered": True,
                                         "items": [{"blocks": []}]}]),
                       "sendRichMessage", "non-empty list of blocks")

    # buttons block
    def test_buttons_block_valid(self):
        p = rich([{"type": "buttons", "align": "center",
                   "buttons": [cb("a"), {"text": "b", "url": "https://x.y"},
                               {"text": "c", "callback_data": "ok",
                                "style": "danger"}]}])
        assert_valid(self, p, "sendRichMessage")

    def test_buttons_block_9_rejected(self):
        btns = [{"text": f"b{i}", "callback_data": f"d{i}"} for i in range(9)]
        assert_invalid(self, rich([{"type": "buttons", "buttons": btns}]),
                       "sendRichMessage", "1-8 buttons")

    def test_buttons_block_0_rejected(self):
        assert_invalid(self, rich([{"type": "buttons", "buttons": []}]),
                       "sendRichMessage", "1-8 buttons")

    def test_buttons_block_8_valid(self):
        btns = [{"text": f"b{i}", "callback_data": f"d{i}"} for i in range(8)]
        assert_valid(self, rich([{"type": "buttons", "buttons": btns}]),
                     "sendRichMessage")

    def test_buttons_block_bad_align_rejected(self):
        assert_invalid(self, rich([{"type": "buttons", "align": "middle",
                                         "buttons": [cb("a")]}]),
                       "sendRichMessage", "must be one of")

    def test_button_text_dict_rejected(self):
        b = {"text": {"type": "bold", "text": "x"}, "callback_data": "d"}
        assert_invalid(self, rich([{"type": "buttons", "buttons": [b]}]),
                       "sendRichMessage", "plain string")

    def test_button_text_list_rejected(self):
        b = {"text": ["x"], "callback_data": "d"}
        assert_invalid(self, rich([{"type": "buttons", "buttons": [b]}]),
                       "sendRichMessage", "plain string")

    def test_button_text_empty_rejected(self):
        b = {"text": "", "callback_data": "d"}
        assert_invalid(self, rich([{"type": "buttons", "buttons": [b]}]),
                       "sendRichMessage", "non-empty")

    def test_button_two_actions_rejected(self):
        b = {"text": "x", "callback_data": "d", "url": "https://x.y"}
        assert_invalid(self, rich([{"type": "buttons", "buttons": [b]}]),
                       "sendRichMessage", "exactly one action")

    def test_button_zero_actions_rejected(self):
        b = {"text": "x"}
        assert_invalid(self, rich([{"type": "buttons", "buttons": [b]}]),
                       "sendRichMessage", "exactly one action")

    def test_button_callback_65_bytes_rejected(self):
        b = {"text": "x", "callback_data": "a" * 65}
        assert_invalid(self, rich([{"type": "buttons", "buttons": [b]}]),
                       "sendRichMessage", "1-64 bytes")

    def test_button_callback_64_bytes_valid(self):
        b = {"text": "x", "callback_data": "a" * 64}
        assert_valid(self, rich([{"type": "buttons", "buttons": [b]}]),
                     "sendRichMessage")

    def test_button_callback_empty_rejected(self):
        b = {"text": "x", "callback_data": ""}
        assert_invalid(self, rich([{"type": "buttons", "buttons": [b]}]),
                       "sendRichMessage", "1-64 bytes")

    def test_button_callback_multibyte_boundary(self):
        ok = {"text": "x", "callback_data": "\U0001F600" * 16}   # 64 bytes
        bad = {"text": "x", "callback_data": "\U0001F600" * 17}  # 68 bytes
        assert_valid(self, rich([{"type": "buttons", "buttons": [ok]}]),
                     "sendRichMessage")
        assert_invalid(self, rich([{"type": "buttons", "buttons": [bad]}]),
                       "sendRichMessage", "1-64 bytes")

    def test_button_bad_style_rejected(self):
        b = {"text": "x", "callback_data": "d", "style": "weird"}
        assert_invalid(self, rich([{"type": "buttons", "buttons": [b]}]),
                       "sendRichMessage", "must be one of")

    # table (live-verified shape: {"type", "cells", "is_compact"?};
    # "rows" 400s with Can't find field "cells")
    def test_table_strict_shape_valid(self):
        t = {"type": "table", "cells": [[{"text": "h"}],
                                        [{"text": ["a", {"type": "bold",
                                                         "text": "b"}]}]]}
        assert_valid(self, rich([t]), "sendRichMessage")

    def test_table_is_compact_valid(self):
        t = {"type": "table", "is_compact": True, "cells": [[{"text": "h"}]]}
        assert_valid(self, rich([t]), "sendRichMessage")

    def test_table_is_header_valid(self):
        t = {"type": "table", "is_compact": True,
             "cells": [[{"text": "h", "is_header": True}]]}
        assert_valid(self, rich([t]), "sendRichMessage")

    def test_table_extra_cell_key_rejected(self):
        t = {"type": "table", "cells": [[{"text": "h", "bogus": 1}]]}
        assert_invalid(self, rich([t]), "sendRichMessage",
                       "unexpected cell fields")

    def test_table_rows_field_rejected(self):
        # The old guessed "rows" field is rejected by the server.
        t = {"type": "table", "rows": [[{"text": "h"}]]}
        assert_invalid(self, rich([t]), "sendRichMessage",
                       "unexpected table fields")

    def test_table_cells_empty_rejected(self):
        assert_invalid(self, rich([{"type": "table", "cells": []}]),
                       "sendRichMessage", "non-empty 2-D list of cells")

    def test_table_extra_block_field_rejected(self):
        t = {"type": "table", "cells": [[{"text": "h"}]], "title": "x"}
        assert_invalid(self, rich([t]), "sendRichMessage",
                       "unexpected table fields")

    # v1.18.0: details and expandable_blockquote are documented shapes
    def test_details_valid(self):
        d = {"type": "details", "summary": "t",
             "blocks": [{"type": "paragraph", "text": "x"}]}
        assert_valid(self, rich([d]), "sendRichMessage")

    def test_details_missing_blocks_rejected(self):
        d = {"type": "details", "summary": "t"}
        assert_invalid(self, rich([d]), "sendRichMessage",
                       "required non-empty list of nested blocks")

    def test_expandable_blockquote_valid(self):
        assert_valid(self, rich([{"type": "expandable_blockquote",
                                        "text": "x"}]), "sendRichMessage")

    # unverified / unknown block types still fail loud

    def test_pull_quote_rejected(self):
        assert_invalid(self, rich([{"type": "pull_quote", "text": "x"}]),
                       "sendRichMessage", "unsupported/unverified")

    def test_photo_block_rejected(self):
        assert_invalid(self, rich([{"type": "photo"}]), "sendRichMessage",
                       "unsupported/unverified")

    def test_unknown_block_type_rejected(self):
        assert_invalid(self, rich([{"type": "carousel"}]), "sendRichMessage",
                       "unsupported/unverified")

    def test_block_not_dict_rejected(self):
        assert_invalid(self, rich(["just a string"]), "sendRichMessage",
                       "must be a dict")


# ---------------------------------------------------------------------------
# Rule 3: RichText
# ---------------------------------------------------------------------------

class TestRichText(unittest.TestCase):
    def para(self, text):
        return rich([{"type": "paragraph", "text": text}])

    def test_plain_str_valid(self):
        assert_valid(self, self.para("hello"), "sendRichMessage")

    def test_empty_list_rejected(self):
        assert_invalid(self, self.para([]), "sendRichMessage",
                       "must be non-empty")

    def test_list_with_bad_element_rejected(self):
        assert_invalid(self, self.para(["ok", 42]), "sendRichMessage",
                       "must be str, non-empty list")

    def test_type_text_node_rejected(self):
        assert_invalid(self, self.para({"type": "text", "text": "x"}),
                       "sendRichMessage", "not a real RichText node")

    def test_unknown_node_type_rejected(self):
        assert_invalid(self, self.para({"type": "blink", "text": "x"}),
                       "sendRichMessage", "unknown RichText node type")

    def test_url_valid(self):
        n = {"type": "url", "text": "label", "url": "https://x.y"}
        assert_valid(self, self.para(n), "sendRichMessage")

    def test_url_missing_url_rejected(self):
        assert_invalid(self, self.para({"type": "url", "text": "label"}),
                       "sendRichMessage", ".url")

    def test_mention_valid(self):
        n = {"type": "mention", "text": "@u", "username": "u"}
        assert_valid(self, self.para(n), "sendRichMessage")

    def test_mention_missing_username_rejected(self):
        assert_invalid(self, self.para({"type": "mention", "text": "@u"}),
                       "sendRichMessage", ".username")

    def test_hashtag_valid(self):
        n = {"type": "hashtag", "text": "#t", "hashtag": "t"}
        assert_valid(self, self.para(n), "sendRichMessage")

    def test_hashtag_missing_hashtag_rejected(self):
        assert_invalid(self, self.para({"type": "hashtag", "text": "#t"}),
                       "sendRichMessage", ".hashtag")

    def test_text_mention_valid(self):
        n = {"type": "text_mention", "text": "name", "user": {"id": 123}}
        assert_valid(self, self.para(n), "sendRichMessage")

    def test_text_mention_missing_user_id_rejected(self):
        n = {"type": "text_mention", "text": "name", "user": {}}
        assert_invalid(self, self.para(n), "sendRichMessage", ".user")

    def test_custom_emoji_valid(self):
        n = {"type": "custom_emoji", "custom_emoji_id": "abc",
             "alternative_text": "x"}
        assert_valid(self, self.para(n), "sendRichMessage")

    def test_custom_emoji_missing_id_rejected(self):
        assert_invalid(self, self.para({"type": "custom_emoji"}),
                       "sendRichMessage", ".custom_emoji_id")

    def test_nested_formatting_valid(self):
        n = {"type": "bold", "text": ["a ", {"type": "italic", "text": "b"}, " c"]}
        assert_valid(self, self.para(n), "sendRichMessage")

    def test_formatted_node_missing_text_rejected(self):
        assert_invalid(self, self.para({"type": "bold"}), "sendRichMessage",
                       "required RichText")

    def test_anchor_valid(self):
        assert_valid(self, self.para({"type": "anchor", "name": "sec1"}),
                     "sendRichMessage")

    def test_anchor_missing_name_rejected(self):
        assert_invalid(self, self.para({"type": "anchor"}), "sendRichMessage",
                       ".name")

    def test_math_expression_valid(self):
        n = {"type": "mathematical_expression", "expression": "x^2"}
        assert_valid(self, self.para(n), "sendRichMessage")

    def test_button_node_valid(self):
        n = {"type": "button",
             "button": {"text": "go", "callback_data": "d"}}
        assert_valid(self, self.para(n), "sendRichMessage")

    def test_button_node_bad_button_rejected(self):
        n = {"type": "button",
             "button": {"text": "go", "callback_data": "d",
                        "url": "https://x.y"}}
        assert_invalid(self, self.para(n), "sendRichMessage", "exactly one action")

    def test_subscript_superscript_valid(self):
        assert_valid(self, self.para({"type": "subscript", "text": "2"}),
                     "sendRichMessage")
        assert_valid(self, self.para({"type": "superscript", "text": "2"}),
                     "sendRichMessage")

    def test_deep_nesting_warns_not_fails(self):
        node = "x"
        for _ in range(20):
            node = {"type": "bold", "text": node}
        errs = validate_outgoing(self.para(node), "sendRichMessage")
        warns = [e for e in errs if e.startswith("WARN: ")]
        hard = [e for e in errs if not e.startswith("WARN: ")]
        self.assertTrue(warns, "expected a depth WARN")
        self.assertEqual(hard, [], f"depth must warn, not fail: {hard}")

    def test_moderate_nesting_valid_no_warn(self):
        node = "x"
        for _ in range(8):
            node = {"type": "bold", "text": node}
        self.assertEqual(validate_outgoing(self.para(node), "sendRichMessage"), [])


# ---------------------------------------------------------------------------
# Rule 4: sendMessage
# ---------------------------------------------------------------------------

class TestSendMessage(unittest.TestCase):
    def test_plain_valid(self):
        assert_valid(self, {"chat_id": 1, "text": "hi"}, "sendMessage")

    def test_missing_text_rejected(self):
        assert_invalid(self, {"chat_id": 1}, "sendMessage", "required non-empty")

    def test_empty_text_rejected(self):
        assert_invalid(self, {"chat_id": 1, "text": ""}, "sendMessage",
                       "required non-empty")

    def test_4096_chars_valid(self):
        assert_valid(self, {"chat_id": 1, "text": "x" * 4096}, "sendMessage")

    def test_4097_chars_rejected(self):
        assert_invalid(self, {"chat_id": 1, "text": "x" * 4097}, "sendMessage",
                       "4096")

    def test_entities_and_parse_mode_rejected(self):
        p = {"chat_id": 1, "text": "hi", "parse_mode": "HTML",
             "entities": [{"type": "bold", "offset": 0, "length": 2}]}
        assert_invalid(self, p, "sendMessage", "must not both be set")

    def test_entities_alone_valid(self):
        p = {"chat_id": 1, "text": "hi",
             "entities": [{"type": "bold", "offset": 0, "length": 2}]}
        assert_valid(self, p, "sendMessage")

    def test_entity_negative_offset_rejected(self):
        p = {"chat_id": 1, "text": "hi",
             "entities": [{"type": "bold", "offset": -1, "length": 2}]}
        assert_invalid(self, p, "sendMessage", "non-negative int")

    def test_entity_str_length_rejected(self):
        p = {"chat_id": 1, "text": "hi",
             "entities": [{"type": "bold", "offset": 0, "length": "2"}]}
        assert_invalid(self, p, "sendMessage", "non-negative int")

    def test_entity_bool_offset_rejected(self):
        p = {"chat_id": 1, "text": "hi",
             "entities": [{"type": "bold", "offset": True, "length": 2}]}
        assert_invalid(self, p, "sendMessage", "non-negative int")

    def test_entity_missing_type_rejected(self):
        p = {"chat_id": 1, "text": "hi",
             "entities": [{"offset": 0, "length": 2}]}
        assert_invalid(self, p, "sendMessage", ".type")

    def test_entities_not_list_rejected(self):
        p = {"chat_id": 1, "text": "hi", "entities": "nope"}
        assert_invalid(self, p, "sendMessage", "must be a list")


# ---------------------------------------------------------------------------
# Rule 5: captions
# ---------------------------------------------------------------------------

class TestCaption(unittest.TestCase):
    def test_caption_1024_valid(self):
        assert_valid(self, {"chat_id": 1, "caption": "x" * 1024}, "sendPhoto")

    def test_caption_1025_rejected(self):
        assert_invalid(self, {"chat_id": 1, "caption": "x" * 1025}, "sendPhoto",
                       "1024")

    def test_caption_non_str_rejected(self):
        assert_invalid(self, {"chat_id": 1, "caption": 42}, "sendPhoto",
                       "must be a string")

    def test_caption_on_unknown_method_checked(self):
        assert_invalid(self, {"caption": "x" * 2000}, "sendVideo", "1024")


# ---------------------------------------------------------------------------
# Rule 6: reply_markup inline keyboards
# ---------------------------------------------------------------------------

class TestInlineKeyboard(unittest.TestCase):
    def kb(self, rows, **kw):
        p = {"chat_id": 1, "text": "hi",
             "reply_markup": {"inline_keyboard": rows}}
        p.update(kw)
        return p

    def test_valid_keyboard(self):
        rows = [[{"text": "a", "callback_data": "d"},
                 {"text": "b", "url": "https://x.y"}]]
        assert_valid(self, self.kb(rows), "sendMessage")

    def test_empty_rows_rejected(self):
        assert_invalid(self, self.kb([]), "sendMessage", "non-empty list")

    def test_empty_row_rejected(self):
        assert_invalid(self, self.kb([[]]), "sendMessage", "non-empty list of buttons")

    def test_button_empty_text_rejected(self):
        assert_invalid(self, self.kb([[{"text": "", "callback_data": "d"}]]),
                       "sendMessage", "non-empty")

    def test_button_text_non_str_rejected(self):
        assert_invalid(self, self.kb([[{"text": 7, "callback_data": "d"}]]),
                       "sendMessage", "plain string")

    def test_button_two_actions_rejected(self):
        rows = [[{"text": "x", "callback_data": "d", "url": "https://x.y"}]]
        assert_invalid(self, self.kb(rows), "sendMessage", "exactly one action")

    def test_button_zero_actions_rejected(self):
        assert_invalid(self, self.kb([[{"text": "x"}]]), "sendMessage",
                       "exactly one action")

    def test_button_callback_65_bytes_rejected(self):
        rows = [[{"text": "x", "callback_data": "a" * 65}]]
        assert_invalid(self, self.kb(rows), "sendMessage", "1-64 bytes")

    def test_button_style_on_classic_keyboard_rejected(self):
        rows = [[{"text": "x", "callback_data": "d", "style": "primary"}]]
        assert_invalid(self, self.kb(rows), "sendMessage", "'style'")

    def test_reply_markup_json_string_valid(self):
        rm = json.dumps({"inline_keyboard": [[{"text": "a", "callback_data": "d"}]]})
        assert_valid(self, {"chat_id": 1, "text": "hi", "reply_markup": rm},
                     "sendMessage")

    def test_reply_markup_bad_json_rejected(self):
        assert_invalid(self, {"chat_id": 1, "text": "hi", "reply_markup": "{bad"},
                       "sendMessage", "not valid JSON")

    def test_keyboard_on_sendrichmessage_markdown_valid(self):
        p = {"chat_id": 1, "rich_message": {"markdown": "**hi**"},
             "reply_markup": {"inline_keyboard": [[cb("a")]]}}
        assert_valid(self, p, "sendRichMessage")


# ---------------------------------------------------------------------------
# Rule 7: editMessageText
# ---------------------------------------------------------------------------

class TestEditMessageText(unittest.TestCase):
    def test_neither_rejected(self):
        assert_invalid(self, {"chat_id": 1, "message_id": 2}, "editMessageText",
                       "'text' OR 'rich_message' required")

    def test_text_only_valid(self):
        assert_valid(self, {"chat_id": 1, "message_id": 2, "text": "new"},
                     "editMessageText")

    def test_rich_message_only_valid(self):
        p = {"chat_id": 1, "message_id": 2,
             "rich_message": {"blocks": [{"type": "paragraph", "text": "x"}]}}
        assert_valid(self, p, "editMessageText")

    def test_rich_message_two_modes_rejected(self):
        p = {"chat_id": 1, "message_id": 2,
             "rich_message": {"markdown": "x",
                              "blocks": [{"type": "paragraph", "text": "x"}]}}
        assert_invalid(self, p, "editMessageText", "exactly one")

    def test_text_too_long_rejected(self):
        p = {"chat_id": 1, "message_id": 2, "text": "x" * 4097}
        assert_invalid(self, p, "editMessageText", "4096")

    def test_empty_text_no_rich_message_rejected(self):
        assert_invalid(self, {"chat_id": 1, "message_id": 2, "text": ""},
                       "editMessageText", "non-empty string")


# ---------------------------------------------------------------------------
# Regression tests for the historical bugs (audit §C)
# ---------------------------------------------------------------------------

class TestRegressions(unittest.TestCase):
    def test_regression_type_text_block_rejected(self):
        # C.15: ron_daemon.rich_text_block emitted {"type":"text"} — the
        # invented shape is rejected (as a block type here; as a RichText
        # node it is rejected too — see TestRichText.test_type_text_node_rejected)
        p = rich([{"type": "text", "text": "hi"}])
        assert_invalid(self, p, "sendRichMessage", "unsupported/unverified")

    def test_regression_markdown_plus_blocks_rejected(self):
        p = {"chat_id": 1, "rich_message": {
            "markdown": "**hi**", "blocks": [{"type": "paragraph", "text": "hi"}]}}
        assert_invalid(self, p, "sendRichMessage", "exactly one")

    def test_regression_empty_pre_rejected(self):
        # C.6: lone closing fence emitted {"type":"pre","text":""}
        p = rich([{"type": "pre", "text": ""}])
        assert_invalid(self, p, "sendRichMessage", "non-empty")

    def test_regression_9_buttons_rejected(self):
        btns = [{"text": f"b{i}", "callback_data": f"d{i}"} for i in range(9)]
        p = rich([{"type": "buttons", "buttons": btns}])
        assert_invalid(self, p, "sendRichMessage", "1-8 buttons")

    def test_regression_65byte_callback_data_rejected(self):
        # 64-byte limit is BYTES, not chars (research §D.4)
        b = {"text": "x", "callback_data": "e" * 65}
        p = rich([{"type": "buttons", "buttons": [b]}])
        assert_invalid(self, p, "sendRichMessage", "1-64 bytes")

    def test_regression_two_action_fields_rejected(self):
        b = {"text": "x", "callback_data": "d", "url": "https://x.y"}
        p = rich([{"type": "buttons", "buttons": [b]}])
        assert_invalid(self, p, "sendRichMessage", "exactly one action")

    def test_regression_button_text_dict_rejected(self):
        # Button labels must be plain strings, never node dicts
        b = {"text": {"type": "bold", "text": "x"}, "callback_data": "d"}
        p = rich([{"type": "buttons", "buttons": [b]}])
        assert_invalid(self, p, "sendRichMessage", "plain string")

    def test_regression_entities_plus_parse_mode_rejected(self):
        p = {"chat_id": 1, "text": "hi", "parse_mode": "HTML",
             "entities": [{"type": "bold", "offset": 0, "length": 2}]}
        assert_invalid(self, p, "sendMessage", "must not both be set")

    def test_regression_4097_char_text_rejected(self):
        assert_invalid(self, {"chat_id": 1, "text": "x" * 4097}, "sendMessage",
                       "4096")


if __name__ == "__main__":
    unittest.main()
