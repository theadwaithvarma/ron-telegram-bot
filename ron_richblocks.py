#!/usr/bin/env python3
"""Telegram Bot API 10.3 rich-message blocks builder.

Builds the *blocks form* of InputRichMessage so buttons render INSIDE the
message bubble (InputRichBlockButtons) instead of under it (reply_markup
inline keyboard).

Every wire shape below was verified against the live Bot API with
sendRichMessage probes on 2026-09-20. Probe trick: an invalid chat_id
(12345) returns "Bad Request: chat not found" when the payload PARSES, and
a parse error otherwise — so shapes can be validated without sending any
visible message.

Verified shapes:
- paragraph:  {"type": "paragraph", "text": RichText}
- blockquote: {"type": "blockquote", "text": RichText}
- pre:        {"type": "pre", "text": str, "language": str}
- list:       {"type": "list", "ordered": bool, "items": [{"text": RichText}]}
- divider:    {"type": "divider"}
- buttons:    {"type": "buttons", "buttons": [RichMessageButton, ...],
               "align": "left"|"center"|"right"}   (ONE row per block, 1-8)
- RichText:   plain str, or a list mixing str and inline nodes
- inline:     {"type": "bold"|"italic"|"code"|"underline"|"strikethrough"|
               "spoiler"|"marked", "text": RichText}
              {"type": "url", "text": RichText, "url": str}   ("url" REQUIRED)
              {"type": "mention"|"hashtag"|"subscript", "text": str}

NOT cracked (probes kept answering "Can't find field"): the heading and
table block shapes. Headings degrade to a bold paragraph, tables to a
monospace pre block with aligned columns — info preserved, zero API risk.

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
            close = s.find(marker, i + len(marker))
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

_FENCE_RE = re.compile(r"^```(\w*)\s*$")
_DIVIDER_RE = re.compile(r"^(---+$|\*\*\*+$|___+)\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_UL_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_OL_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")


def _table_to_text(tlines):
    """Degrade a markdown table to aligned monospace text (pre block)."""
    rows = []
    for ln in tlines:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if cells and all(re.match(r"^:?-{2,}:?$", c) for c in cells):
            continue  # separator row: |---|---|
        rows.append([re.sub(r"\s+", " ", c) for c in cells])
    if not rows:
        return "\n".join(tlines)
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    colw = [max(len(r[j]) for r in rows) for j in range(width)]
    return "\n".join(
        "  ".join(c.ljust(colw[j]) for j, c in enumerate(r)).rstrip()
        for r in rows)


def _is_table_row(s):
    # "||spoiler||" starts AND ends with "|" — not a table row.
    return (s.startswith("|") and s.endswith("|") and not s.startswith("||")
            and s.count("|") >= 3)


def markdown_to_blocks(md):
    """Convert a Telegram-Markdown reply into rich-message blocks.

    Covers what the bot's replies actually use: paragraphs, ## headings
    (degraded to bold — the heading block shape never parsed in probes),
    > quotes, - / 1. lists, ``` code fences, --- dividers, tables
    (degraded to monospace), and all inline formatting via parse_inline.
    Never raises on weird input — worst case it degrades to plain
    paragraph blocks.
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
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1  # skip closing fence (or run past EOF, fine)
            blk = {"type": "pre", "text": "\n".join(code).strip("\n")}
            if m.group(1):
                blk["language"] = m.group(1)
            blocks.append(blk)
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
        # > quote block
        if s.startswith(">"):
            flush_para()
            q = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                q.append(re.sub(r"^>\s?", "", lines[i].strip()))
                i += 1
            blocks.append({"type": "blockquote",
                           "text": parse_inline("\n".join(q))})
            continue
        # - / * / 1. lists
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
                items.append({"text": parse_inline(mm.group(1))})
                i += 1
            if items:
                blocks.append({"type": "list", "ordered": ordered,
                               "items": items})
            continue
        # | table | -> monospace pre block
        if _is_table_row(s):
            flush_para()
            tlines = []
            while i < len(lines) and _is_table_row(lines[i].strip()):
                tlines.append(lines[i].strip())
                i += 1
            blocks.append({"type": "pre",
                           "text": _table_to_text(tlines)})
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
