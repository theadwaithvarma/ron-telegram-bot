#!/usr/bin/env python3
"""Strict local validator for Telegram outgoing payloads.

Phase 3 of the rich-format hardening work. This module only ADDS validation;
it is not wired into the daemon (a later stage does that) and it reads
nothing else in the repo.

Single entry point::

    validate_outgoing(payload, method) -> list[str]

``payload`` is the params dict that would be sent to the Bot API ``method``
(e.g. ``"sendRichMessage"``). Returns a list of violation strings; an empty
list means the payload is valid.

Entries starting with ``"WARN: "`` are ADVISORY only (RichText nesting deeper
than 16 levels — the official nesting ceiling is unverified, so we warn
rather than fail). Everything else is a hard violation.

Grounding: /tmp/rich-format-research.md (Bot API 10.3, official shapes) and
/tmp/rich-format-audit.md (the concrete emission bugs in this repo). The
headline traps this validator exists to catch:

* ``{"type": "text"}`` wrappers and ``RichTextPre`` do NOT exist in the
  official RichText union — plain strings / lists only (historical bug C.15:
  ``ron_daemon.rich_text_block`` emitted ``{"type": "text"}``).
* ``InputRichMessage`` demands EXACTLY ONE of ``blocks``/``html``/``markdown``.
* ``callback_data`` is 1-64 BYTES of UTF-8, not characters.
* ``entities`` and ``parse_mode`` must never be combined.
* ``sendMessage`` text is 1-4096 chars; captions are 1-1024 chars.
* ``RichMessageButton`` / ``InlineKeyboardButton``: EXACTLY ONE action field.
* Block shapes below are the LIVE-VERIFIED ones (2026-09-21 shape probes):
  blockquote takes ``blocks`` (``{"text": ...}`` 400s RICH_MESSAGE_EMPTY);
  list items are ``{"blocks": [...]}`` (``{"text": ...}`` 400s);
  tables take ``cells`` (``"rows"`` 400s "Can't find field \"cells\"").

Deliberate strict choices (so a later wiring stage knows what it is opting
into):

* ``divider`` blocks take no fields beyond ``type``.
* ``table`` blocks are allowed ONLY in the live-verified
  ``{type, cells: [[{text: RichText}]]}`` shape (plus optional ``is_compact``
  bool, which is officially verified). ``details`` / ``expandable_blockquote``
  / ``pull_quote`` / media blocks fail loud — we don't emit them and their
  exact input shapes are unverified.
* ``style`` on classic ``reply_markup`` inline-keyboard buttons is a
  violation: ``style`` is a ``RichMessageButton`` (in-bubble) field; the
  official ``InlineKeyboardButton`` has no such field.
* Entity ``offset``/``length`` are UTF-16 code units per the official docs; we
  can only verify they are non-negative ints — the violation text says so.

stdlib only; pure functions; no network; no secrets. Never raises: an internal
error is reported as a violation string instead.
"""

import json

# ---------------------------------------------------------------------------
# Limits & enums (Bot API 10.3, official)
# ---------------------------------------------------------------------------

MAX_TEXT_LEN = 4096          # sendMessage / editMessageText text, chars
MAX_CAPTION_LEN = 1024       # caption on media sends, chars
MAX_CALLBACK_BYTES = 64      # callback_data, BYTES of UTF-8
MAX_BUTTONS_PER_BLOCK = 8    # InputRichBlockButtons
MAX_NESTING = 16             # RichText nesting: warn (don't fail) past this

# Official RichText union (Bot API 10.3). NOTE: there is no "text" and no
# "pre" — those shapes are invented and the server rejects them.
_RICH_PLAIN_FORMAT_NODES = frozenset({
    "bold", "italic", "underline", "strikethrough", "spoiler", "marked",
    "code", "subscript", "superscript", "email_address", "phone_number",
    "bank_card_number", "date_time", "reference", "reference_link",
})
RICH_TEXT_TYPES = _RICH_PLAIN_FORMAT_NODES | frozenset({
    "url", "mention", "hashtag", "cashtag", "bot_command", "text_mention",
    "custom_emoji", "mathematical_expression", "anchor", "anchor_link",
    "button",
})

# Allowed extra fields per typed RichText node (beyond "type").
_NODE_ALLOWED_FIELDS = {n: {"text"} for n in _RICH_PLAIN_FORMAT_NODES}
_NODE_ALLOWED_FIELDS.update({
    "url": {"text", "url"},
    "mention": {"text", "username"},
    "hashtag": {"text", "hashtag"},
    "cashtag": {"text", "cashtag"},
    "bot_command": {"text", "command"},
    "text_mention": {"text", "user"},
    "custom_emoji": {"custom_emoji_id", "alternative_text"},
    "mathematical_expression": {"expression"},
    "anchor": {"name"},
    "anchor_link": {"text", "name"},
    "button": {"button"},
})

# Exactly-one-of action fields, shared by RichMessageButton (in-bubble) and
# InlineKeyboardButton (reply_markup). Official contract: "Exactly one of the
# fields other than text and style must be used".
BUTTON_ACTIONS = frozenset({
    "callback_data", "url", "web_app", "login_url",
    "switch_inline_query", "switch_inline_query_current_chat",
    "switch_inline_query_chosen_chat", "copy_text", "disabled",
})
BUTTON_STYLES = frozenset({"primary", "success", "danger", "link"})
BUTTON_BLOCK_ALIGN = frozenset({"left", "center", "right"})
HEADING_SIZES = frozenset({1, 2, 3, 4, 5, 6})


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _is_int(x):
    """True for real ints; bool is NOT an int for our purposes."""
    return isinstance(x, int) and not isinstance(x, bool)


def _check_nonempty_str(value, path, out):
    if not isinstance(value, str) or not value:
        out.append(f"{path}: required non-empty string, got {value!r}")


def _check_message_text(text, path, out):
    """Classic 1-4096 char text rule (sendMessage / editMessageText)."""
    if not isinstance(text, str) or not text:
        out.append(f"{path}: required non-empty string, got {text!r}")
    elif len(text) > MAX_TEXT_LEN:
        out.append(f"{path}: {len(text)} chars exceeds the 4096-char limit")


# ---------------------------------------------------------------------------
# RichText (recursive)
# ---------------------------------------------------------------------------

def _check_richtext(node, path, out, depth=0):
    """Validate one RichText value: str | non-empty list | typed node."""
    if isinstance(node, str):
        return  # plain string is always valid RichText
    if depth > MAX_NESTING:
        out.append(
            f"WARN: {path}: RichText nesting deeper than {MAX_NESTING} levels "
            "(official ceiling unverified; stopped descending)")
        return
    if isinstance(node, list):
        if not node:
            out.append(f"{path}: RichText list must be non-empty")
            return
        for i, el in enumerate(node):
            _check_richtext(el, f"{path}[{i}]", out, depth + 1)
        return
    if not isinstance(node, dict):
        out.append(f"{path}: RichText must be str, non-empty list, or a typed "
                   f"node dict; got {type(node).__name__}")
        return
    t = node.get("type")
    if t == "text":
        out.append(f"{path}: 'type:\"text\"' is not a real RichText node — use a "
                   "plain string or a list (this invented shape is rejected by "
                   "the server)")
        return
    if t not in RICH_TEXT_TYPES:
        out.append(f"{path}: unknown RichText node type {t!r} — fail loud "
                   "(not in the official Bot API 10.3 union)")
        return
    _check_typed_node(node, t, path, out, depth)


def _check_richtext_required(node, path, out, depth=0, nonempty=False):
    if node is None:
        out.append(f"{path}: required RichText, got missing/None")
        return
    if nonempty and isinstance(node, str) and node == "":
        out.append(f"{path}: RichText must be non-empty here "
                   "(empty pre blocks are rejected by the server)")
        return
    _check_richtext(node, path, out, depth)


def _check_typed_node(node, t, path, out, depth):
    allowed = _NODE_ALLOWED_FIELDS[t]
    extra = set(node) - {"type"} - allowed
    if extra:
        out.append(f"{path}: unexpected fields {sorted(extra)} on {t!r} node")
    if "text" in allowed:
        _check_richtext_required(node.get("text"), f"{path}.text", out, depth + 1)
    if t == "url":
        _check_nonempty_str(node.get("url"), f"{path}.url", out)
    elif t == "mention":
        _check_nonempty_str(node.get("username"), f"{path}.username", out)
    elif t == "hashtag":
        _check_nonempty_str(node.get("hashtag"), f"{path}.hashtag", out)
    elif t == "cashtag":
        _check_nonempty_str(node.get("cashtag"), f"{path}.cashtag", out)
    elif t == "bot_command":
        _check_nonempty_str(node.get("command"), f"{path}.command", out)
    elif t == "text_mention":
        user = node.get("user")
        if not isinstance(user, dict) or not _is_int(user.get("id")):
            out.append(f"{path}.user: required dict with int 'id', got {user!r}")
    elif t == "custom_emoji":
        _check_nonempty_str(node.get("custom_emoji_id"),
                            f"{path}.custom_emoji_id", out)
    elif t == "mathematical_expression":
        _check_nonempty_str(node.get("expression"), f"{path}.expression", out)
    elif t in ("anchor", "anchor_link"):
        _check_nonempty_str(node.get("name"), f"{path}.name", out)
    elif t == "button":
        b = node.get("button")
        if not isinstance(b, dict):
            out.append(f"{path}.button: required RichMessageButton dict, "
                       f"got {b!r}")
        else:
            _check_button(b, f"{path}.button", out, allow_style=True)


# ---------------------------------------------------------------------------
# Buttons (RichMessageButton + InlineKeyboardButton share the action rule)
# ---------------------------------------------------------------------------

def _check_button(btn, path, out, allow_style):
    if not isinstance(btn, dict):
        out.append(f"{path}: button must be a dict, got {type(btn).__name__}")
        return
    text = btn.get("text")
    if not isinstance(text, str) or not text:
        out.append(f"{path}.text: required non-empty plain string (button "
                   f"labels must be plain text), got {text!r}")
    actions = [a for a in BUTTON_ACTIONS if btn.get(a) is not None]
    if len(actions) != 1:
        detail = ", ".join(sorted(actions)) if actions else "none"
        out.append(f"{path}: exactly one action field required "
                   f"(got {len(actions)}: {detail}); choose from "
                   f"{sorted(BUTTON_ACTIONS)}")
    for a in actions:
        if a == "callback_data":
            v = btn[a]
            if not isinstance(v, str):
                out.append(f"{path}.callback_data: must be a string, "
                           f"got {type(v).__name__}")
            else:
                n = len(v.encode("utf-8"))
                if not 1 <= n <= MAX_CALLBACK_BYTES:
                    out.append(f"{path}.callback_data: must be 1-64 bytes "
                               f"UTF-8, got {n} bytes")
    if "style" in btn and btn["style"] is not None:
        if not allow_style:
            out.append(f"{path}.style: 'style' is not a field of classic "
                       "InlineKeyboardButton (it belongs to in-bubble "
                       "RichMessageButton)")
        elif btn["style"] not in BUTTON_STYLES:
            out.append(f"{path}.style: must be one of "
                       f"{sorted(BUTTON_STYLES)}, got {btn['style']!r}")
    allowed = {"text"} | BUTTON_ACTIONS | ({"style"} if allow_style else set())
    extra = set(btn) - allowed
    if extra:
        out.append(f"{path}: unexpected button fields {sorted(extra)}")


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------

def _check_block(block, path, out):
    if not isinstance(block, dict):
        out.append(f"{path}: block must be a dict, got {type(block).__name__}")
        return
    t = block.get("type")

    if t == "paragraph":
        extra = set(block) - {"type", "text"}
        if extra:
            out.append(f"{path}: unexpected fields {sorted(extra)} on {t!r} block")
        _check_richtext_required(block.get("text"), f"{path}.text", out)

    elif t == "footer":
        extra = set(block) - {"type", "text"}
        if extra:
            out.append(f"{path}: unexpected fields {sorted(extra)} on {t!r} block")
        _check_richtext_required(block.get("text"), f"{path}.text", out)

    elif t == "blockquote":
        # Live-verified (2026-09-21): blockquote takes nested "blocks",
        # NOT "text" — the {"text": ...} shape 400s RICH_MESSAGE_EMPTY.
        extra = set(block) - {"type", "blocks"}
        if extra:
            out.append(f"{path}: unexpected fields {sorted(extra)} on "
                       f"'blockquote' block (the server rejects a 'text' "
                       f"field here)")
        nested = block.get("blocks")
        if not isinstance(nested, list) or not nested:
            out.append(f"{path}.blocks: required non-empty list of nested "
                       f"blocks, got {nested!r}")
        else:
            for i, nb in enumerate(nested):
                _check_block(nb, f"{path}.blocks[{i}]", out)

    elif t == "heading":
        extra = set(block) - {"type", "text", "size"}
        if extra:
            out.append(f"{path}: unexpected fields {sorted(extra)} on 'heading' block")
        _check_richtext_required(block.get("text"), f"{path}.text", out)
        if "size" in block and block["size"] is not None:
            if not _is_int(block["size"]) or block["size"] not in HEADING_SIZES:
                out.append(f"{path}.size: must be int 1-6, got {block['size']!r}")

    elif t == "pre":
        extra = set(block) - {"type", "text", "language"}
        if extra:
            out.append(f"{path}: unexpected fields {sorted(extra)} on 'pre' block")
        _check_richtext_required(block.get("text"), f"{path}.text", out,
                                 nonempty=True)
        if "language" in block and block["language"] is not None:
            if not isinstance(block["language"], str):
                out.append(f"{path}.language: must be a string, "
                           f"got {block['language']!r}")

    elif t == "divider":
        if set(block) != {"type"}:
            out.append(f"{path}: divider takes no fields beyond 'type', got "
                       f"extra {sorted(set(block) - {'type'})}")

    elif t == "list":
        extra = set(block) - {"type", "ordered", "items"}
        if extra:
            out.append(f"{path}: unexpected fields {sorted(extra)} on 'list' block")
        if "ordered" not in block or not isinstance(block["ordered"], bool):
            out.append(f"{path}.ordered: must be bool, got {block.get('ordered')!r}")
        items = block.get("items")
        if not isinstance(items, list) or not items:
            out.append(f"{path}.items: must be a non-empty list, got {items!r}")
        else:
            # Live-verified (2026-09-21): items are InputRichBlockListItem
            # = {"blocks": [...]}. The {"text": ...} shape 400s
            # RICH_MESSAGE_EMPTY.
            for i, item in enumerate(items):
                ip = f"{path}.items[{i}]"
                if not isinstance(item, dict) or set(item) != {"blocks"}:
                    out.append(f"{ip}: item must be {{\"blocks\": [blocks]}}, "
                               f"got {item!r}")
                    continue
                nested = item["blocks"]
                if not isinstance(nested, list) or not nested:
                    out.append(f"{ip}.blocks: must be a non-empty list of "
                               f"blocks, got {nested!r}")
                    continue
                for j, nb in enumerate(nested):
                    _check_block(nb, f"{ip}.blocks[{j}]", out)

    elif t == "buttons":
        extra = set(block) - {"type", "buttons", "align"}
        if extra:
            out.append(f"{path}: unexpected fields {sorted(extra)} on 'buttons' block")
        buttons = block.get("buttons")
        if not isinstance(buttons, list):
            out.append(f"{path}.buttons: must be a list of 1-8 buttons, "
                       f"got {type(buttons).__name__}")
        elif not buttons or len(buttons) > MAX_BUTTONS_PER_BLOCK:
            out.append(f"{path}.buttons: 1-8 buttons required, got {len(buttons)}")
        else:
            for i, b in enumerate(buttons):
                _check_button(b, f"{path}.buttons[{i}]", out, allow_style=True)
        if "align" in block and block["align"] is not None:
            if block["align"] not in BUTTON_BLOCK_ALIGN:
                out.append(f"{path}.align: must be one of "
                           f"{sorted(BUTTON_BLOCK_ALIGN)}, got {block['align']!r}")

    elif t == "table":
        _check_table(block, path, out)

    else:
        out.append(f"{path}: unsupported/unverified block type {t!r} — fail loud "
                   "(only paragraph/blockquote/footer/heading/pre/divider/list/"
                   "buttons and the strict table shape are allowed)")


def _check_table(block, path, out):
    """Live-verified table shape: {type, cells: [[{text: RichText}]]}.

    The server rejects a "rows" field ("Can't find field \"cells\"").
    """
    extra = set(block) - {"type", "cells", "is_compact"}
    if extra:
        out.append(f"{path}: table allows only type/cells/is_compact, got extra "
                   f"{sorted(extra)}")
    if "is_compact" in block and block["is_compact"] is not None:
        if not isinstance(block["is_compact"], bool):
            out.append(f"{path}.is_compact: must be bool, "
                       f"got {block['is_compact']!r}")
    cells = block.get("cells")
    if not isinstance(cells, list) or not cells:
        out.append(f"{path}.cells: must be a non-empty 2-D list of cells, "
                   f"got {cells!r}")
        return
    for r, row in enumerate(cells):
        if not isinstance(row, list) or not row:
            out.append(f"{path}.cells[{r}]: must be a non-empty list of cells, "
                       f"got {row!r}")
            continue
        for c, cell in enumerate(row):
            cp = f"{path}.cells[{r}][{c}]"
            if not isinstance(cell, dict) or set(cell) != {"text"}:
                out.append(f"{cp}: cell must be {{\"text\": RichText}}, "
                           f"got {cell!r}")
            else:
                _check_richtext_required(cell.get("text"), f"{cp}.text", out)


# ---------------------------------------------------------------------------
# InputRichMessage (blocks | html | markdown — exactly one)
# ---------------------------------------------------------------------------

def _check_rich_message(rm, path, out):
    modes = [m for m in ("blocks", "html", "markdown") if rm.get(m) is not None]
    if len(modes) != 1:
        detail = ", ".join(modes) if modes else "none"
        out.append(f"{path}: exactly one of blocks/html/markdown must be set "
                   f"(got {len(modes)}: {detail})")
        return
    mode = modes[0]
    if mode == "blocks":
        blocks = rm["blocks"]
        if not isinstance(blocks, list) or not blocks:
            out.append(f"{path}.blocks: must be a non-empty list of blocks, "
                       f"got {blocks!r}")
            return
        for i, b in enumerate(blocks):
            _check_block(b, f"{path}.blocks[{i}]", out)
    else:
        val = rm[mode]
        if not isinstance(val, str) or not val:
            out.append(f"{path}.{mode}: must be a non-empty string, got {val!r}")


# ---------------------------------------------------------------------------
# reply_markup / caption (cross-method)
# ---------------------------------------------------------------------------

def _check_inline_keyboard(kb, path, out):
    if not isinstance(kb, list) or not kb:
        out.append(f"{path}: rows must be a non-empty list, got {kb!r}")
        return
    for r, row in enumerate(kb):
        if not isinstance(row, list) or not row:
            out.append(f"{path}[{r}]: row must be a non-empty list of buttons, "
                       f"got {row!r}")
            continue
        for b, btn in enumerate(row):
            _check_button(btn, f"{path}[{r}][{b}]", out, allow_style=False)


def _check_reply_markup(params, method, out):
    if "reply_markup" not in params or params["reply_markup"] is None:
        return
    rm = params["reply_markup"]
    if isinstance(rm, str):
        # urlencoded transport carries reply_markup as a JSON string
        try:
            rm = json.loads(rm)
        except (ValueError, TypeError):
            out.append(f"{method}.reply_markup: not valid JSON: {rm!r}"[:200])
            return
    if not isinstance(rm, dict):
        out.append(f"{method}.reply_markup: must be a dict (or JSON string), "
                   f"got {type(rm).__name__}")
        return
    kb = rm.get("inline_keyboard")
    if kb is None:
        return  # other markup kinds are out of scope
    _check_inline_keyboard(kb, f"{method}.reply_markup.inline_keyboard", out)


def _check_caption(params, method, out):
    if "caption" in params and params["caption"] is not None:
        cap = params["caption"]
        if not isinstance(cap, str):
            out.append(f"{method}.caption: must be a string, "
                       f"got {type(cap).__name__}")
        elif len(cap) > MAX_CAPTION_LEN:
            out.append(f"{method}.caption: {len(cap)} chars exceeds the "
                       "1024-char caption limit")


# ---------------------------------------------------------------------------
# Method handlers
# ---------------------------------------------------------------------------

def _validate_send_rich_message(params, method, out):
    rm = params.get("rich_message")
    if not isinstance(rm, dict):
        got = "missing" if rm is None else type(rm).__name__
        out.append(f"{method}.rich_message: required dict, got {got}")
        return
    _check_rich_message(rm, f"{method}.rich_message", out)
    _check_reply_markup(params, method, out)


def _validate_send_message(params, method, out):
    _check_message_text(params.get("text"), f"{method}.text", out)
    has_entities = "entities" in params and params["entities"] is not None
    has_parse_mode = "parse_mode" in params and params["parse_mode"] is not None
    if has_entities and has_parse_mode:
        out.append(f"{method}: 'entities' and 'parse_mode' must not both be set "
                   "(entities are ignored when parse_mode is present)")
    if has_entities:
        ents = params["entities"]
        if not isinstance(ents, list):
            out.append(f"{method}.entities: must be a list, "
                       f"got {type(ents).__name__}")
        else:
            for i, e in enumerate(ents):
                ep = f"{method}.entities[{i}]"
                if not isinstance(e, dict):
                    out.append(f"{ep}: entity must be a dict, "
                               f"got {type(e).__name__}")
                    continue
                if not isinstance(e.get("type"), str) or not e.get("type"):
                    out.append(f"{ep}.type: required non-empty string")
                for k in ("offset", "length"):
                    v = e.get(k)
                    # NOTE: offsets are UTF-16 code units per the official
                    # docs; without the text we can only check the ints.
                    if not _is_int(v) or v < 0:
                        out.append(f"{ep}.{k}: must be a non-negative int "
                                   f"(UTF-16 code units), got {v!r}")
    _check_caption(params, method, out)
    _check_reply_markup(params, method, out)


def _validate_edit_message_text(params, method, out):
    text = params.get("text")
    rm = params.get("rich_message")
    has_text = isinstance(text, str) and text != ""
    has_rm = isinstance(rm, dict)
    if not has_text and not has_rm:
        if text is not None or rm is not None:
            out.append(f"{method}: 'text' must be a non-empty string or "
                       "'rich_message' a valid dict")
        else:
            out.append(f"{method}: 'text' OR 'rich_message' required "
                       "(Bot API 10.3)")
        return
    if has_text:
        _check_message_text(text, f"{method}.text", out)
    if has_rm:
        _check_rich_message(rm, f"{method}.rich_message", out)
    _check_reply_markup(params, method, out)


def _validate_send_photo(params, method, out):
    _check_caption(params, method, out)
    _check_reply_markup(params, method, out)


_METHODS = {
    "sendRichMessage": _validate_send_rich_message,
    "sendMessage": _validate_send_message,
    "editMessageText": _validate_edit_message_text,
    "sendPhoto": _validate_send_photo,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def validate_outgoing(payload, method):
    """Validate an outgoing Telegram payload.

    Args:
        payload: params dict for the Bot API call.
        method: Bot API method name, e.g. "sendRichMessage".

    Returns:
        list[str]: violation strings; empty means valid. Entries prefixed
        "WARN: " are advisory (deep RichText nesting), not hard failures.
    """
    try:
        out = []
        if not isinstance(payload, dict):
            return [f"{method}: payload must be a dict of params, got "
                    f"{type(payload).__name__}"]
        handler = _METHODS.get(method)
        if handler is None:
            # Unknown/other methods: still guard the cross-cutting rules.
            _check_caption(payload, method, out)
            _check_reply_markup(payload, method, out)
            return out
        handler(payload, method, out)
        return out
    except Exception as exc:  # never raise out of a validator
        return [f"{method}: validator internal error: {exc!r}"]
