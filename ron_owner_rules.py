#!/usr/bin/env python3
"""Owner rulebook: standing behavioral rules the owner teaches the bot.

The owner teaches in his own DM with explicit trigger phrases —
"remember: ...", "remember that/to ...", "from now on, ...",
"new rule: ...", "note: ..." — so ordinary chat never becomes a rule
by accident. Only the owner's user id may add or remove rules (gated
in ron_daemon); rules then apply to the bot's replies in EVERY chat it
speaks in, via prompt injection in ron_ai.chat/chat_stream.

Rules are behavior config, not conversation memory: they live in
owner_rules.json (live dir, never copied on deploy, never committed)
and are separate from the per-chat isolated memory in ron_memory —
nothing learned here leaks between users' chats.

Pure logic + tiny JSON store. No network. PATH is a module-level
variable so tests can point it at a temp file.
"""
import json
import os
import re
import time

DIR = os.path.expanduser("~/workspace/telegram-bot")
PATH = os.path.join(DIR, "owner_rules.json")
MAX_RULES = 30
MAX_RULE_CHARS = 200

_TEACH_RES = (
    re.compile(r"^remember\s*:\s*(.+)$", re.I | re.S),
    re.compile(r"^remember\s+(that|to)\s+(.+)$", re.I | re.S),
    re.compile(r"^from now on[,:]?\s+(.+)$", re.I | re.S),
    re.compile(r"^new rule\s*:\s*(.+)$", re.I | re.S),
    re.compile(r"^note(?:\s+to self)?\s*:\s*(.+)$", re.I | re.S),
)


def detect_teach(text):
    """Return the rule text if this DM line is an owner teach request.

    Deliberately narrow: only the explicit trigger phrases above count.
    "remember this ..." (per-chat memory) and ordinary chat mentioning
    "remember" do NOT match.
    """
    s = (text or "").strip()
    if not s:
        return None
    for rx in _TEACH_RES:
        m = rx.match(s)
        if m:
            rule = m.groups()[-1].strip()
            # Collapse internal whitespace/newlines to one clean line.
            rule = re.sub(r"\s+", " ", rule)
            if rule:
                return rule[:MAX_RULE_CHARS]
    return None


def load_rules():
    """Return the rule list (each: {id, text, ts}). Never raises."""
    try:
        with open(PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [r for r in data
                    if isinstance(r, dict) and r.get("text")]
    except (OSError, ValueError):
        pass
    return []


def _save(rules):
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PATH)


def add_rule(text):
    """Add a rule. Returns (rule_dict, None) or (None, error_str)."""
    text = (text or "").strip()
    if not text:
        return None, "Empty rule — nothing learned."
    rules = load_rules()
    if len(rules) >= MAX_RULES:
        return None, (f"Rulebook is full ({MAX_RULES} rules) — "
                      f"/forgetrule one first.")
    # De-dupe (case-insensitive): re-teaching the same rule is a no-op.
    for r in rules:
        if r["text"].lower() == text.lower():
            return None, "Already in the rulebook — nothing new learned."
    rule = {"id": int(time.time() * 1000),
            "text": text[:MAX_RULE_CHARS],
            "ts": time.time()}
    rules.append(rule)
    _save(rules)
    return rule, None


def remove_rule(n):
    """Remove rule number n (1-based, as shown by /rules). Returns the
    removed text, or None if n is out of range."""
    rules = load_rules()
    if not isinstance(n, int) or n < 1 or n > len(rules):
        return None
    removed = rules.pop(n - 1)
    _save(rules)
    return removed.get("text")


def rules_block():
    """Prompt-injection text for ron_ai, or '' when there are no rules."""
    rules = load_rules()
    if not rules:
        return ""
    lines = ["[Owner's standing rules — always follow these:]"]
    lines += [f"{i}. {r['text']}" for i, r in enumerate(rules, 1)]
    return "\n".join(lines) + "\n"
