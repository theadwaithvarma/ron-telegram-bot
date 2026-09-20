#!/usr/bin/env python3
"""Telegram Bot API 10.3 rich-message blocks builder.

Builds the *blocks form* of InputRichMessage so buttons render INSIDE the
message bubble (InputRichBlockButtons) instead of under it (reply_markup
inline keyboard).

Every wire shape below was verified against the live Bot API with
sendRichMessage probes on 2026-09-21 (see /tmp/harness/results.md).
The 2026-09-20 probe shapes for blockquote/list/table were WRONG
(guessed) — the live API 400s them; the corrected shapes below are the
empirically accepted ones.

Verified shapes:
- paragraph:  {"type": "paragraph", "text": RichText}
- blockquote: {"type": "blockquote", "blocks": [nested blocks, ...]}
              (NOT {"text": ...} — that 400s with RICH_MESSAGE_EMPTY)
- pre:        {"type": "pre", "text": str, "language"?: str}
              (language optional; EMPTY text 400s RICH_MESSAGE_CONTENT_REQUIRED)
- list:       {"type": "list", "ordered": bool,
               "items": [{"blocks": [{"type": "paragraph", "text": RichText}]}]}
              (items are InputRichBlockListItem = {"blocks": [...]} —
               NOT {"text": ...}, which 400s with RICH_MESSAGE_EMPTY)
- divider:    {"type": "divider"}
- table:      {"type": "table", "cells": [[{"text": RichText}, ...], ...],
               "is_compact"?: bool}
              (field is "cells", a 2-D array — NOT "rows"; "rows" 400s with
               "Can't find field \"cells\"")
- buttons:    {"type": "buttons", "buttons": [RichMessageButton, ...],
               "align": "left"|"center"|"right"}   (ONE row per block, 1-8)
- RichText:   plain str, or a list mixing str and inline nodes
- inline:     {"type": "bold"|"italic"|"code"|"underline"|"strikethrough"|
               "spoiler"|"marked", "text": RichText}
              {"type": "url", "text": RichText, "url": str}   ("url" REQUIRED)

NOT emitted (shapes unverified — fail loud locally rather than guess):
heading blocks (degrade to bold paragraph), expandable_blockquote,
pull_quote, details, media blocks. Mention/hashtag/subscript parsing is
not implemented — #/@ stay plain text on the blocks path (the markdown
path entity-parses them), by design.

Pure functions, no network. Safe to import anywhere.
"""
import re

__all__ = ["parse_inline", "text_blocks", "markdown_to_blocks"]


# ---------------------------------------------------------------------------
# Inline RichText: Telegram Markdown -> RichText (str or node list)
# ---------------------------------------------------------------------------

# Two-char markers first so "**" wins over "*", "__" over "_".
_TWO_CHAR = (
    ("**", "bold"),
    ("__", "underline"),
    ("~~", "strikethrough"),
    ("||", "spoiler"),
    ("==", "marked"),          # ==highlighted==
)
_ONE_CHAR = (
    ("`", "code"),
    ("*", "italic"),
    ("_", "italic"),
)
_ESCAPABLE = set("*_`~|=[!#>-")


def _merge(parts):
    """Merge adjacent strings; unwrap a lone element to keep it simple."""
    out = []
    for p in parts:
        if isinstance(p, str) and out and isinstance(out[-1], str):
            out[-1] += p
        else:
            out.append(p)
    if not out:
        return ""
    if len(out) == 1:
        return out[0]
    return out


def parse_inline(s):
    """Parse Telegram Markdown inline formatting into RichText.

    Returns a plain str when there is no formatting, else a list mixing
    str and {"type", "text"[,"url"]} nodes. Unbalanced markers are left
    literal — never emit a broken node.
    """
    parts, buf = [], []

    def flush():
        if buf:
            parts.append("".join(buf))
            buf.clear()

    i, n = 0, len(s or "")
    while i < n:
        ch = s[i]
        # Backslash escape: \* \_ \` \~ \| \= \[ \! \# \> \-
        if ch == "\\" and i + 1 < n and s[i + 1] in _ESCAPABLE:
            buf.append(s[i + 1])
            i += 2
            continue
        # [text](url)
        if ch == "[":
            m = re.match(r"\[([^\]\n]+?)\]\((https?://[^)\s]+)\)", s[i:])
            if m:
                flush()
                parts.append({"type": "url", "text": parse_inline(m.group(1)),
                              "url": m.group(2)})
                i += m.end()
                continue
        matched = False
        for marker, typ in _TWO_CHAR + _ONE_CHAR:
            if not s.startswith(marker, i):
                continue
            # A single-char marker must not be half of a double one.
            if len(marker) == 1 and s.startswith(marker * 2, i):
                continue
            # Lone "_" inside a word (like_this) is not italic.
            if marker == "_" and i > 0 and (s[i - 1].isalnum()):
                continue
            # Find the closing marker. A single-char candidate is skipped
            # when it is half of a double marker — mirror of the open-side
            # guard above. Without this, '*a **b** c*' pairs the opening
            # '*' with the first '*' of the inner '**', silently flattening
            # the bold node into italic (audit C.1).
            close = -1
            j = i + len(marker)
            while True:
                j = s.find(marker, j)
                if j == -1:
                    break
                if len(marker) == 1 and (
                        s.startswith(marker * 2, j)
                        or (j > i and s.startswith(marker * 2, j - 1))):
                    j += 1
                    continue
                close = j
                break
            if close == -1:
                continue
            inner = s[i + len(marker):close]
            if not inner or (typ != "code" and "\n" in inner):
                continue
            if marker == "_" and close + 1 < n and s[close + 1].isalnum():
                continue
            flush()
            if typ == "code":
                # Code contents are literal — no nested parsing.
                parts.append({"type": "code", "text": inner})
            else:
                parts.append({"type": typ, "text": parse_inline(inner)})
            i = close + len(marker)
            matched = True
            break
        if not matched:
            buf.append(ch)
            i += 1
    flush()
    return _merge(parts)


# ---------------------------------------------------------------------------
# Blocks: text -> paragraph blocks
# ---------------------------------------------------------------------------

def text_blocks(text):
    """Plain text -> one paragraph block per blank-line-separated chunk."""
    chunks = [c.strip("\n") for c in re.split(r"\n\s*\n", text or "")]
    blocks = [{"type": "paragraph", "text": c} for c in chunks if c.strip()]
    return blocks or [{"type": "paragraph", "text": ""}]


# ---------------------------------------------------------------------------
# Blocks: Telegram Markdown -> block list (for messages that carry buttons)
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```([^`\s]*)\s*$")
_DIVIDER_RE = re.compile(r"^(---+$|\*\*\*+$|___+)\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_UL_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_OL_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")


def _split_cells(s):
    """Split a table row on '|' without shattering ||spoiler|| spans.

    Spoiler markers contain the cell separator, so "||x||" is hidden
    behind a placeholder before the split and restored after.
    """
    hidden = []

    def _hide(m):
        hidden.append(m.group(0))
        return "\x00%d\x00" % (len(hidden) - 1)

    masked = re.sub(r"\|\|.*?\|\|", _hide, s)
    parts = [c.strip() for c in masked.strip().strip("|").split("|")]
    return [re.sub(r"\x00(\d+)\x00",
                   lambda m: hidden[int(m.group(1))], p)
            for p in parts]


def _is_table_row(s):
    # "||spoiler||" starts AND ends with "|" — not a table row.
    return (s.startswith("|") and s.endswith("|") and not s.startswith("||")
            and s.count("|") >= 3)


def markdown_to_blocks(md):
    """Convert a Telegram-Markdown reply into rich-message blocks.

    Covers what the bot's replies actually use: paragraphs, ## headings
    (degraded to bold paragraph — heading blocks are unverified locally),
    > quotes, - / 1. lists, ``` code fences, --- dividers, tables (real
    table blocks with inline-parsed cells), and all inline formatting via
    parse_inline. Never raises on weird input — worst case it degrades to
    plain paragraph blocks. Never emits an empty pre block.
    """
    lines = (md or "").split("\n")
    blocks, para = [], []

    def flush_para():
        if para:
            text = "\n".join(para).strip("\n")
            if text.strip():
                blocks.append({"type": "paragraph",
                               "text": parse_inline(text)})
            para.clear()

    i = 0
    while i < len(lines):
        line, s = lines[i], lines[i].strip()
        # ``` code fence
        m = _FENCE_RE.match(s)
        if m:
            flush_para()
            code = []
            i += 1
            closed = False
            while i < len(lines):
                if lines[i].strip().startswith("```"):
                    closed = True
                    break
                code.append(lines[i])
                i += 1
            if closed:
                i += 1  # skip the closing fence
            # Unclosed fence: only the code collected so far is code; the
            # fence line + content become a pre block ONLY when there is
            # actual code content. Never emit an empty pre block — the
            # server 400s it (RICH_MESSAGE_CONTENT_REQUIRED). An empty
            # fence carries no content, so it is dropped silently.
            body = "\n".join(code).strip("\n")
            if body.strip():
                blk = {"type": "pre", "text": body}
                if m.group(1):
                    blk["language"] = m.group(1)
                blocks.append(blk)
            elif not closed:
                # Unclosed fence with no code content: keep the fence
                # line as a plain paragraph. Never an empty pre block —
                # the server 400s it (RICH_MESSAGE_CONTENT_REQUIRED).
                blocks.append({"type": "paragraph", "text": s})
            # Closed-but-empty fence (```\n```): carries no content at
            # all — dropped rather than sent as an empty pre.
            continue
        # --- divider
        if _DIVIDER_RE.match(s):
            flush_para()
            blocks.append({"type": "divider"})
            i += 1
            continue
        # ## heading -> bold paragraph (heading block shape unknown)
        m = _HEADING_RE.match(s)
        if m:
            flush_para()
            blocks.append({"type": "paragraph",
                           "text": [{"type": "bold",
                                     "text": parse_inline(m.group(2))}]})
            i += 1
            continue
        # > quote block — verified shape: {"type": "blockquote",
        # "blocks": [nested blocks]}. The {"text": ...} shape 400s with
        # RICH_MESSAGE_EMPTY (harness B7 + shape probes, 2026-09-21).
        if s.startswith(">"):
            flush_para()
            q = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                q.append(re.sub(r"^>\s?", "", lines[i].strip()))
                i += 1
            blocks.append({"type": "blockquote", "blocks": [
                {"type": "paragraph",
                 "text": parse_inline("\n".join(q))}]})
            continue
        # - / * / 1. lists — items are InputRichBlockListItem =
        # {"blocks": [...]}. The {"text": ...} shape 400s with
        # RICH_MESSAGE_EMPTY (harness B10/B11 + shape probes, 2026-09-21).
        m = _UL_RE.match(line) or _OL_RE.match(line)
        if m:
            flush_para()
            ordered = bool(_OL_RE.match(line))
            items = []
            while i < len(lines):
                mm = (_OL_RE.match(lines[i]) if ordered
                      else _UL_RE.match(lines[i]))
                if not mm:
                    break
                items.append({"blocks": [
                    {"type": "paragraph",
                     "text": parse_inline(mm.group(1))}]})
                i += 1
            if items:
                blocks.append({"type": "list", "ordered": ordered,
                               "items": items})
            continue
        # | table | — real table block. The server takes "cells" (a 2-D
        # array of {"text": RichText}); the old monospace-pre degradation
        # is gone (harness B15 + shape probes, 2026-09-21). The
        # separator row (|---|---|) is skipped; ragged rows are padded
        # with empty cells; cell content is inline-parsed.
        if _is_table_row(s):
            flush_para()
            rows = []
            while i < len(lines) and _is_table_row(lines[i].strip()):
                cells = _split_cells(lines[i].strip())
                if cells and all(re.match(r"^:?-{2,}:?$", c)
                                 for c in cells):
                    i += 1
                    continue  # separator row: |---|---|
                rows.append([{"text": parse_inline(
                    re.sub(r"\s+", " ", c))} for c in cells])
                i += 1
            if rows:
                width = max(len(r) for r in rows)
                cells2d = [r + [{"text": ""}] * (width - len(r))
                           for r in rows]
                blocks.append({"type": "table", "cells": cells2d})
            continue
        # blank line = paragraph break
        if not s:
            flush_para()
            i += 1
            continue
        para.append(line)
        i += 1
    flush_para()
    return blocks or [{"type": "paragraph", "text": ""}]
