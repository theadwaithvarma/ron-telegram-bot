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
               "spoiler"|"marked"|"subscript"|"superscript", "text": RichText}
              (subscript from ~x~, superscript from ^x^ — the two-char
               strikethrough ~~x~~ still wins over single ~, and unbalanced
               ~/^ stay literal)
              {"type": "url", "text": RichText, "url": str}   ("url" REQUIRED)
              {"type": "reference_link", "text": "[id]", "reference_name": id}
              (footnote ref, markdown [^id])
              {"type": "reference", "text": RichText, "name": id}
              (footnote definition, from a trailing "[^id]: note" line)
              {"type": "anchor", "name": id}   (zero-width jump target;
               auto-attached to every ## heading)
              {"type": "anchor_link", "text": RichText, "anchor_name": id}
              (in-message jump link, markdown [text](#id))
              {"type": "date_time", "text": str, "unix_time": int}
              (markdown {{YYYY-MM-DD HH:MM}}, parsed as IST wall time;
               renders in each reader's timezone)

NOT emitted (shapes unverified — fail loud locally rather than guess):
media blocks. Mention/hashtag parsing is
not implemented — #/@ stay plain text on the blocks path (the markdown
path entity-parses them), by design.

Pure functions, no network. Safe to import anywhere.
"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

__all__ = ["parse_inline", "text_blocks", "markdown_to_blocks",
           "needs_blocks_form"]


# ---------------------------------------------------------------------------
# Date-time markers: {{2026-09-22 18:00}} -> RichTextDateTime (v1.20.0)
# ---------------------------------------------------------------------------

# Naive datetimes inside {{...}} are the model's wall-clock time, which the
# prompt pins to IST (the user's timezone). The unix_time is what the
# client localizes per reader; text is the honest fallback for clients
# that don't render date_time nodes.
_IST = ZoneInfo("Asia/Kolkata")
_DT_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d",
               "%d %b %Y %H:%M", "%d %b %Y",
               "%d %B %Y %H:%M", "%d %B %Y")
_DT_MARKER_RE = re.compile(r"\{\{([^}\n]{1,80})\}\}")
_FOOTNOTE_REF_RE = re.compile(r"\[\^([\w-]+)\]")
_FOOTNOTE_DEF_RE = re.compile(r"^\[\^([\w-]+)\]:\s*(.*)$")
_ANCHOR_LINK_RE = re.compile(r"\[([^\]\n]+?)\]\(#([\w-]+)\)")
_HEADING_ANCHOR_RE = re.compile(r"\s*\{#([\w-]+)\}\s*$")


def _parse_datetime(s):
    """Parse a {{...}} marker body as IST wall time.

    Returns (display_text, unix_time) or None when the body is not a
    recognized datetime — the marker is then left literal.
    """
    s = (s or "").strip()
    for fmt in _DT_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    else:
        return None
    ts = int(dt.replace(tzinfo=_IST).timestamp())
    disp = f"{dt.day} {dt.strftime('%b')} {dt.year}"
    if "%H" in fmt:
        h12 = dt.hour % 12 or 12
        ampm = "AM" if dt.hour < 12 else "PM"
        disp += f", {h12}:{dt.minute:02d} {ampm} IST"
    return disp, ts


def _slugify(s):
    """Heading text -> anchor name: lowercase, spaces/underscores to
    hyphens, other punctuation dropped. Documented in the system prompt
    so the model can predict TOC link targets."""
    s = re.sub(r"[\s_]+", "-", (s or "").lower())
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "section"


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
    # v1.20.0: single ~ is subscript, single ^ is superscript. The two-char
    # ~~ strikethrough above is matched FIRST, so ~~x~~ still parses as
    # strikethrough and only lone ~x~ becomes subscript. Neither char had
    # any inline meaning before, so no existing syntax is disturbed.
    ("~", "subscript"),
    ("^", "superscript"),
)
_ESCAPABLE = set("*_`~|=[!#>-^")


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
            # v1.20.0: [^id] footnote reference -> reference_link node.
            # Checked before the url branch: "[^1](https://x)" reads as a
            # footnote ref followed by literal "(https://x)".
            fm = _FOOTNOTE_REF_RE.match(s, i)
            if fm:
                flush()
                fid = fm.group(1)
                parts.append({"type": "reference_link", "text": f"[{fid}]",
                              "reference_name": fid})
                i = fm.end()
                continue
            m = re.match(r"\[([^\]\n]+?)\]\((https?://[^)\s]+)\)", s[i:])
            if m:
                flush()
                parts.append({"type": "url", "text": parse_inline(m.group(1)),
                              "url": m.group(2)})
                i += m.end()
                continue
            # v1.20.0: [text](#anchor) in-message jump link -> anchor_link.
            am = _ANCHOR_LINK_RE.match(s, i)
            if am:
                flush()
                parts.append({"type": "anchor_link",
                              "text": parse_inline(am.group(1)),
                              "anchor_name": am.group(2)})
                i = am.end()
                continue
        # v1.20.0: {{2026-09-22 18:00}} date-time marker -> date_time node
        # (renders in each reader's timezone). Unparseable bodies stay
        # literal — e.g. Jinja {{ x }} in prose is untouched.
        if ch == "{":
            dm = _DT_MARKER_RE.match(s, i)
            if dm:
                parsed = _parse_datetime(dm.group(1))
                if parsed is not None:
                    flush()
                    disp, ts = parsed
                    parts.append({"type": "date_time", "text": disp,
                                  "unix_time": ts})
                    i = dm.end()
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


# Inline node types with no Markdown rendering: if a reply carries any of
# these, the server-side markdown path would show raw markers, so the
# message must go out in blocks form. (v1.20.0: subscript/superscript,
# date_time, footnote reference nodes, anchors and anchor links.)
_INLINE_BLOCKS_ONLY = frozenset({
    "subscript", "superscript", "date_time",
    "reference", "reference_link", "anchor", "anchor_link",
})


def _richtext_has(node, types):
    """True when any nested RichText node has a type in `types`."""
    if isinstance(node, str):
        return False
    if isinstance(node, dict):
        if node.get("type") in types:
            return True
        return any(_richtext_has(v, types) for v in node.values())
    if isinstance(node, list):
        return any(_richtext_has(v, types) for v in node)
    return False


def needs_blocks_form(blocks):
    """True when the block list contains something the server's Markdown
    parser cannot render, so the daemon must use the blocks form.

    Tables, checklists, headings, details, expandable quotes, pull quotes
    and math have no Markdown representation in sendRichMessage — they
    would arrive as literal text. Paragraphs, dividers, pre fences, plain
    lists and plain quotes render fine through the markdown path (which
    also entity-parses #/@, so we prefer it whenever nothing richer is
    present). Inline nodes in _INLINE_BLOCKS_ONLY (v1.20.0) likewise have
    no markdown rendering — e.g. ~sub~ would arrive literally — so any
    block carrying one forces the blocks form too.
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
        if _richtext_has(b, _INLINE_BLOCKS_ONLY):
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
    (real heading blocks, size 1-6; every heading carries a zero-width
    anchor node — explicit via ## Title {#my-id} or auto-derived from the
    heading text — so [text](#my-id) table-of-contents links work),
    > quotes (long ones become expandable_blockquote), >>> pull quotes
    (real pullquote blocks), $$math$$ or fenced $$ blocks (real math
    expressions), <details><summary> expandable sections,
    - / 1. lists, - [ ] / - [x] checklists (real checkbox items),
    ``` code fences (with language), --- dividers, tables (real compact
    table blocks, first row flagged as header cells), [^id] footnote refs
    with trailing [^id]: note definitions (native reference nodes),
    ~sub~ / ^sup^ (native subscript/superscript), {{YYYY-MM-DD HH:MM}}
    (native date_time, parsed as IST), and all inline formatting via
    parse_inline. Never raises on weird input — worst case it degrades to
    plain paragraph blocks. Never emits an empty pre block.
    """
    raw_lines = (md or "").split("\n")
    # v1.20.0: footnote definitions ("[^id]: note") are collected up front
    # and removed from the flow; they render as a footnote section at the
    # end of the message, in order of first reference.
    lines, footnote_defs, footnote_order = [], {}, []
    for ln in raw_lines:
        fm = _FOOTNOTE_DEF_RE.match(ln.strip())
        if fm:
            fid = fm.group(1)
            if fid not in footnote_defs:
                footnote_order.append(fid)
            footnote_defs[fid] = fm.group(2).strip()
        else:
            lines.append(ln)
    blocks, para = [], []
    used_anchors = set()

    def take_anchor(name):
        """Dedupe anchor names: setup, setup-2, ..."""
        base, n = name, 2
        while name in used_anchors:
            name = f"{base}-{n}"
            n += 1
        used_anchors.add(name)
        return name

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
        # v1.20.0: every heading carries a zero-width anchor node so
        # [text](#id) TOC links can jump to it. Explicit id via
        # "## Title {#my-id}"; otherwise auto-derived from the heading
        # text (lowercase, spaces -> hyphens — the prompt documents this
        # rule so the model can predict link targets).
        m = _HEADING_RE.match(s)
        if m:
            flush_para()
            title = m.group(2)
            am = _HEADING_ANCHOR_RE.search(title)
            if am:
                aid = take_anchor(am.group(1))
                title = title[:am.start()].rstrip()
            else:
                aid = take_anchor(_slugify(title))
            text = parse_inline(title)
            anchor_node = {"type": "anchor", "name": aid}
            if isinstance(text, str):
                text = [anchor_node, text] if text else [anchor_node]
            else:
                text = [anchor_node] + text
            blocks.append({"type": "heading", "text": text,
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
    # v1.20.0: footnote definitions render as a footnote section at the
    # end of the message — one paragraph per note, each a native
    # "reference" node the [^id] reference_links jump to. Ordered by
    # first reference in the body; unreferenced definitions are kept
    # (dropping content is worse than an extra note).
    if footnote_defs:
        body_scan = "\n".join(lines)
        ref_order = []
        for fid in _FOOTNOTE_REF_RE.findall(body_scan):
            if fid not in ref_order:
                ref_order.append(fid)
        for fid in ref_order + [f for f in footnote_order
                                if f not in ref_order]:
            note = footnote_defs[fid]
            blocks.append({
                "type": "paragraph",
                "text": [{"type": "reference",
                          "text": parse_inline(note) if note else "",
                          "name": fid}],
            })
    return blocks or [{"type": "paragraph", "text": ""}]
