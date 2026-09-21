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
               "is_compact": True}
              (field is "cells", a 2-D array — NOT "rows"; "rows" 400s with
               "Can't find field \"cells\"". First-row cells carry
               "is_header": True; is_compact renders the compact table.)
- checklist:  {"type": "list", "ordered": False,
               "items": [{"blocks": [{"type": "paragraph",
                                      "text": RichText}],
                          "has_checkbox": True, "is_checked": bool}]}
- heading:    {"type": "heading", "text": RichText, "size": 1-6}
              (InputRichBlockSectionHeading, official docs)
- details:    {"type": "details", "summary": RichText,
               "blocks": [nested blocks]}   (is_open omitted = collapsed)
- expandable_blockquote:
              {"type": "expandable_blockquote", "text": RichText}
              (official docs shape; for long quotes)
- buttons:    {"type": "buttons", "buttons": [RichMessageButton, ...],
               "align": "left"|"center"|"right"}   (ONE row per block, 1-8)
- pullquote:  {"type": "pullquote", "text": RichText}
              (verified shape via live harness 2026-09-21; markdown ">>>")
- math:       {"type": "mathematical_expression", "expression": str}
              (verified shape via live harness 2026-09-21;
               markdown "$$expr$$" or a fenced $$ block)
- RichText:   plain str, or a list mixing str and inline nodes
- inline:     {"type": "bold"|"italic"|"code"|"underline"|"strikethrough"|
               "spoiler"|"marked", "text": RichText}
              {"type": "url", "text": RichText, "url": str}   ("url" REQUIRED)

NOT emitted (shapes unverified — fail loud locally rather than guess):
media blocks. Mention/hashtag/subscript parsing is
not implemented — #/@ stay plain text on the blocks path (the markdown
path entity-parses them), by design.

Pure functions, no network. Safe to import anywhere.
"""
import re

__all__ = ["parse_inline", "text_blocks", "markdown_to_blocks",
           "needs_blocks_form"]


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
_CHECKLIST_RE = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+(.*)$")
_DETAILS_OPEN_RE = re.compile(r"<details\b[^>]*>", re.I)
_DETAILS_CLOSE_RE = re.compile(r"</details\s*>", re.I)
_SUMMARY_RE = re.compile(r"<summary\b[^>]*>(.*?)</summary\s*>", re.I | re.S)
# Human/test shorthand: a standalone "Tap to expand: <title>" line followed
# by the expandable content. Only treated as a block when it starts a
# paragraph (previous line blank / start of input).
_EXPAND_MARKER_RE = re.compile(
    r"^(tap to expand|tap for details|show more|read more)\s*:?\s*(.*)$",
    re.I)
# Quotes longer than this become expandable_blockquote instead of a tall
# static blockquote. Threshold is a local UX choice, not an API rule.
_LONG_QUOTE_CHARS = 400


def needs_blocks_form(blocks):
    """True when the block list contains something the server's Markdown
    parser cannot render, so the daemon must use the blocks form.

    Tables, checklists, headings, details, expandable quotes, pull quotes
    and math have no Markdown representation in sendRichMessage — they
    would arrive as literal text. Paragraphs, dividers, pre fences, plain
    lists and plain quotes render fine through the markdown path (which
    also entity-parses #/@, so we prefer it whenever nothing richer is
    present).
    """
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t in ("table", "details", "heading", "expandable_blockquote",
                 "pullquote", "mathematical_expression"):
            return True
        if t == "list" and any(
                isinstance(it, dict) and it.get("has_checkbox")
                for it in b.get("items", []) or []):
            return True
    return False


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
    (real heading blocks, size 1-6), > quotes (long ones become
    expandable_blockquote), >>> pull quotes (real pullquote blocks),
    $$math$$ or fenced $$ blocks (real math expressions),
    <details><summary> expandable sections,
    - / 1. lists, - [ ] / - [x] checklists (real checkbox items),
    ``` code fences (with language), --- dividers, tables (real compact
    table blocks, first row flagged as header cells), and all inline
    formatting via parse_inline. Never raises on weird input — worst case
    it degrades to plain paragraph blocks. Never emits an empty pre block.
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
        # <details><summary>…</summary>…</details> expandable section.
        # Collected across lines with depth counting so nested <details>
        # pair correctly; an UNCLOSED <details> auto-closes at end of
        # input (better than leaking raw tags as a paragraph).
        if _DETAILS_OPEN_RE.search(s):
            flush_para()
            chunk = [line]
            # The closing tag may sit on the same line as the opening one.
            depth = (len(_DETAILS_OPEN_RE.findall(line))
                     - len(_DETAILS_CLOSE_RE.findall(line)))
            i += 1
            while i < len(lines) and depth > 0:
                chunk.append(lines[i])
                depth += (len(_DETAILS_OPEN_RE.findall(lines[i]))
                          - len(_DETAILS_CLOSE_RE.findall(lines[i])))
                i += 1
            raw = "\n".join(chunk)
            msum = _SUMMARY_RE.search(raw)
            summary = msum.group(1).strip() if msum else ""
            # All <summary> tags are consumed here (outer summary extracted
            # above); the recursive parse below then sees clean inner
            # <details> blocks, which fall back to "Details" if summary-less.
            # This guarantees no literal tags ever leak into paragraphs.
            body = _SUMMARY_RE.sub("", raw)
            body = _DETAILS_OPEN_RE.sub("", body)
            body = _DETAILS_CLOSE_RE.sub("", body).strip("\n")
            inner = markdown_to_blocks(body) if body.strip() else []
            blocks.append({
                "type": "details",
                "summary": (parse_inline(summary) if summary else "Details"),
                "blocks": inner or [{"type": "paragraph", "text": ""}],
            })
            continue
        # "Tap to expand: <title>" shorthand — a paragraph-starting marker
        # line followed by the expandable content up to the next blank line.
        # The prompt teaches <details> first; this is the human shorthand.
        mm = _EXPAND_MARKER_RE.match(s)
        if mm and not para:
            summary = mm.group(2).strip() or "Details"
            i += 1
            content = []
            while i < len(lines) and lines[i].strip():
                content.append(lines[i])
                i += 1
            body = "\n".join(content).strip()
            inner = markdown_to_blocks(body) if body else []
            blocks.append({
                "type": "details",
                "summary": parse_inline(summary),
                "blocks": inner or [{"type": "paragraph", "text": ""}],
            })
            continue
        # --- divider
        if _DIVIDER_RE.match(s):
            flush_para()
            blocks.append({"type": "divider"})
            i += 1
            continue
        # $$math$$ fence or single-line $$expr$$ — real math block
        # (InputRichBlockMathExpression, verified shape 2026-09-21).
        if s.startswith("$$"):
            flush_para()
            rest = s[2:]
            if rest.endswith("$$") and len(rest) > 2:
                expr = rest[:-2].strip()
                i += 1
            else:
                ml = []
                if rest.strip():
                    ml.append(rest.strip())
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("$$"):
                    ml.append(lines[i].rstrip())
                    i += 1
                if i < len(lines):
                    closing = lines[i].strip()[2:]
                    if closing:
                        ml.append(closing)
                    i += 1
                expr = "\n".join(ml).strip()
            blocks.append({"type": "mathematical_expression",
                           "expression": expr})
            continue
        # ## heading -> real heading block (InputRichBlockSectionHeading,
        # official docs: type "heading", text RichText, size 1-6).
        m = _HEADING_RE.match(s)
        if m:
            flush_para()
            blocks.append({"type": "heading",
                           "text": parse_inline(m.group(2)),
                           "size": len(m.group(1))})
            i += 1
            continue
        # >>> pull quote — verified shape: {"type": "pullquote",
        # "text": RichText} (live harness 2026-09-21). Checked BEFORE the
        # plain ">" branch so the longer prefix wins.
        if s.startswith(">>>"):
            flush_para()
            pq = []
            while i < len(lines) and lines[i].strip().startswith(">>>"):
                pq.append(re.sub(r"^>>>\s?", "", lines[i].strip()))
                i += 1
            blocks.append({"type": "pullquote",
                           "text": parse_inline("\n".join(pq))})
            continue
        # > quote block — verified shape: {"type": "blockquote",
        # "blocks": [nested blocks]}. The {"text": ...} shape 400s with
        # RICH_MESSAGE_EMPTY (harness B7 + shape probes, 2026-09-21).
        # Long quotes collapse into an expandable_blockquote instead of a
        # wall of quoted text.
        if s.startswith(">"):
            flush_para()
            q = []
            # >>> pull-quote lines are NOT quote continuations — the
            # pullquote branch above owns them; stop before swallowing one.
            while (i < len(lines) and lines[i].strip().startswith(">")
                   and not lines[i].strip().startswith(">>>")):
                q.append(re.sub(r"^>\s?", "", lines[i].strip()))
                i += 1
            joined = "\n".join(q)
            if len(joined) > _LONG_QUOTE_CHARS:
                blocks.append({"type": "expandable_blockquote",
                               "text": parse_inline(joined)})
            else:
                blocks.append({"type": "blockquote", "blocks": [
                    {"type": "paragraph",
                     "text": parse_inline(joined)}]})
            continue
        # - [ ] / - [x] checklists — real checkbox items (docs-verified
        # InputRichBlockListItem fields has_checkbox/is_checked). Checked
        # BEFORE the plain list regex below: "- [ ] task" would otherwise
        # match as a bullet whose text is "[ ] task".
        cm = _CHECKLIST_RE.match(line)
        if cm:
            flush_para()
            items = []
            while i < len(lines):
                cmm = _CHECKLIST_RE.match(lines[i])
                if not cmm:
                    break
                items.append({
                    "blocks": [{"type": "paragraph",
                                "text": parse_inline(cmm.group(2))}],
                    "has_checkbox": True,
                    "is_checked": cmm.group(1).lower() == "x",
                })
                i += 1
            if items:
                blocks.append({"type": "list", "ordered": False,
                               "items": items})
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
                # Checklist lines ("- [ ] task") are their own block kind —
                # don't swallow them into a plain bullet list.
                if _CHECKLIST_RE.match(lines[i]):
                    break
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
                # Pipe tables lead with a header row: flag its non-empty
                # cells as header cells; compact rendering for the table.
                for c in cells2d[0]:
                    if c.get("text"):
                        c["is_header"] = True
                blocks.append({"type": "table", "cells": cells2d,
                               "is_compact": True})
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
