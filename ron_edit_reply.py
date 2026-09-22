#!/usr/bin/env python3
"""User-requested message editing (v1.27.0): when the user replies to one of
the bot's own messages asking to change it ("make it shorter", "fix the
typo", ...), the model revises the message in place via editMessageText
instead of sending a new reply.

Trailer contract (documented in the system prompt):
  <full revised message text>
  [edit]
      -> the daemon edits the replied-to bot message with the revised text.
  [edit: <full revised text>]
      -> inline form; the body is the revised text (no `]` allowed inside).

The revised text must be the COMPLETE message (not a diff) — it replaces
the original. The daemon strips every other trailer from the revised text
before editing, fires remaining media trailers (e.g. [react:]) against the
edited message, and falls back to a normal new reply when the edit fails
(message too old, not the bot's message, etc.).

Parsing is pure here; the network call lives in ron_daemon (send_healed
already supports editMessageText with self-healing retries). Nothing in
this module imports ron_daemon, so there is no import cycle.
"""
import re

__all__ = ["has_edit_trailer", "extract_revised_text"]

_COLON_RE = re.compile(r"\[edit:\s*([^\]]+?)\s*\]", re.I | re.S)
_BARE_RE = re.compile(r"\[edit\]", re.I)


def has_edit_trailer(reply):
    """True when the reply asks for an in-place edit ([edit] / [edit:])."""
    return bool(_COLON_RE.search(reply or "") or _BARE_RE.search(reply or ""))


def extract_revised_text(reply):
    """Return the revised message text, or None when no [edit] trailer.

    [edit: <text>] -> the body. Bare [edit] -> "" (the caller takes the
    whole reply minus every trailer as the revised text).
    """
    m = _COLON_RE.search(reply or "")
    if m:
        return m.group(1).strip()
    if _BARE_RE.search(reply or ""):
        return ""
    return None
