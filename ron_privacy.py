#!/usr/bin/env python3
"""Phase 4 privacy flows — pure logic, no network, no Telegram API.

Two flows, both approval-via-private-DM (Adwaith's call, 2026-09-20: never
ask in the group — even the ask leaks metadata and puts the person on the
spot publicly):

1. Disclosure: the bot holds a fact about person P learned in a DM with P.
   The topic comes up in a group P is in -> the bot DMs P privately with the
   fact + Yes/No buttons. Yes -> post the fact in the group (once) and
   record consent so it never asks again for that fact in that group.
   No/timeout/unreachable -> silent.
2. Oversharing guard: Adwaith asks something personal in a group -> the bot
   DMs him privately for approval instead of answering publicly. Yes ->
   answer in the group (once). No/timeout -> silent.

All functions here are pure (data in, data out) so they unit-test without
a bot token. The daemon wires them to send/callback side effects.
"""
import re

# ---------------------------------------------------------------------------
# Yes/No classification for approval replies (DM text or group text)
# ---------------------------------------------------------------------------
_APPROVE_RE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|go ahead|share it|do it|answer it|"
    r"please do|fine|ok then)\b", re.I)
_DENY_RE = re.compile(
    r"\b(no|nope|don't|do not|keep it private|not now|never mind|nvm|"
    r"cancel|drop it|stop)\b", re.I)


def classify_response(text):
    """'approve' | 'deny' | None. Deny wins on conflict (safer default)."""
    t = (text or "").strip()
    if not t or len(t) > 120:
        return None
    deny = bool(_DENY_RE.search(t))
    approve = bool(_APPROVE_RE.search(t))
    # Deny always wins on conflict — safer default for a privacy gate.
    if deny:
        return "deny"
    if approve:
        return "approve"
    return None


# ---------------------------------------------------------------------------
# Personal-content tripwires (oversharing guard). Deterministic — no LLM.
# ---------------------------------------------------------------------------
_PERSONAL_RES = [
    re.compile(r"\+?\d[\d][\d\s\-().]{8,}\d"),          # phone-ish
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),             # email
    re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"),           # 12-digit ID-ish
    re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),              # PAN-ish
    re.compile(r"\b\d{2,4}[-/ ]\d{2,4}[-/ ]\d{2,4}\b"),  # date-ish DOB
]
_PERSONAL_KW = [
    "my phone", "my number", "my email", "my address", "my salary",
    "my health", "my medical", "my bank", "account number", "my password",
    "my aadhaar", "my pan", "my girlfriend", "my boyfriend", "my relationship",
    "my income", "my weight", "my disease", "my doctor", "my location",
    "where i live", "my home", "my otp", "my credit card", "my debit card",
    "my upi", "my parents", "my family",
]


def looks_personal(text):
    """True when a message looks like it asks about personal/private matters."""
    t = (text or "")
    if any(rx.search(t) for rx in _PERSONAL_RES):
        return True
    low = t.lower()
    return any(kw in low for kw in _PERSONAL_KW)


# ---------------------------------------------------------------------------
# Disclosure relevance gating — deterministic, conservative (no fishing)
# ---------------------------------------------------------------------------
def topic_overlap_score(text, fact_text):
    """Shared significant words, weighted by distinctiveness."""
    from ron_memory import content_words
    shared = content_words(text) & content_words(fact_text)
    return sum(2 if len(w) >= 8 else 1 for w in shared)


def topic_matches(text, fact_text, threshold=3):
    """True when the conversation genuinely touches the fact's topic."""
    return topic_overlap_score(text, fact_text) >= threshold


def find_disclosable_facts(mem, group_chat_id, message_text):
    """Candidate (user_id, fact) disclosures for a group message.

    For each member seen in the group, look at facts learned in a DM with
    that member (chat scope = str(user_id)) and keep the ones whose topic
    genuinely overlaps the message. Each candidate carries a "consented"
    flag: consented ones may be disclosed without re-asking; unconsented
    ones need the private-DM approval flow. Pure matching — the caller
    decides ask vs disclose vs skip, and never posts from here.
    """
    cands = []
    for uid in mem.member_ids(group_chat_id):
        try:
            facts = mem.dm_facts(uid)
        except Exception:
            continue
        for f in facts:
            if not topic_matches(message_text, f["text"]):
                continue
            try:
                consented = mem.has_consent(group_chat_id, uid, f["id"])
            except Exception:
                consented = False
            cands.append({"user_id": int(uid), "fact": f,
                          "consented": bool(consented)})
    return cands


# ---------------------------------------------------------------------------
# DM ask messages + inline keyboards (callback_data < 64 bytes)
# ---------------------------------------------------------------------------
def approval_keyboard(pending_id):
    # Classic inline keyboard — the fallback path for the approval DM.
    # (Kept byte-identical: text + callback_data only.)
    return {"inline_keyboard": [
        [{"text": b["text"], "callback_data": b["callback_data"]} for b in row]
        for row in approval_button_rows(pending_id)]}


def approval_button_rows(pending_id):
    """Button rows for the privacy approval ask, with semantic styles.

    Single source of truth for both render paths: the classic inline
    keyboard above, and the Bot API 10.3 rich-buttons block built by the
    daemon. Yes = success (green), No = danger (red).
    """
    return [[
        {"text": "✅ Yes, share", "callback_data": f"mem:go:{pending_id}",
         "style": "success"},
        {"text": "❌ Keep private", "callback_data": f"mem:no:{pending_id}",
         "style": "danger"},
    ]]


_CB_RE = re.compile(r"^mem:(go|no):(\d+)$")


def parse_memory_callback(data):
    """('go'|'no', pending_id) or None."""
    m = _CB_RE.match((data or "").strip())
    return (m.group(1), int(m.group(2))) if m else None


def disclosure_dm_text(group_name, fact_text):
    return (
        f"In {group_name}, something you told me privately came up.\n\n"
        f"My private note from our chat:\n\"{fact_text}\"\n\n"
        "Okay to share it there? I'll only ask once per group — "
        "your call is remembered."
    )


def overshare_dm_text(group_name, question):
    q = (question or "").strip()
    if len(q) > 220:
        q = q[:220] + "…"
    return (
        f"You asked \"{q}\" in {group_name} — that looks personal, "
        "and the whole group would see my answer.\n\n"
        "Answer it there anyway?"
    )


# ---------------------------------------------------------------------------
# Prompt-level backstop (defense in depth behind the tripwires)
# ---------------------------------------------------------------------------
GROUP_PRIVACY_RULE = (
    "\nPrivacy rule: if a message asks for personal or private information "
    "— phone, email, address, ID numbers, health, finances, passwords, "
    "relationships — do NOT answer it in the group. Say you'll DM them "
    "about it privately instead. Never reveal private details in a group "
    "chat, and never steer the conversation toward someone's private "
    "matters."
)


# ---------------------------------------------------------------------------
# Natural-language memory commands ("remember this ...", "forget ...")
# ---------------------------------------------------------------------------
_REMEMBER_RE = re.compile(
    r"^(remember|note down|note|save)\s+(this|that)\s*[:\-–]?\s*(.+)$",
    re.I | re.S)
_FORGET_ALL_RE = re.compile(r"^forget\s+(everything|all|it all)\s*$", re.I)
_FORGET_RE = re.compile(r"^forget\s+(that|this|it|about)\s*(.*)$", re.I | re.S)


def parse_remember(text):
    """Fact text for 'remember this: ...' or None."""
    m = _REMEMBER_RE.match((text or "").strip())
    if not m:
        return None
    fact = m.group(3).strip()
    return fact if len(fact) >= 4 else None


def parse_forget(text):
    """('all', None) | ('query', q) | ('latest', None) | None."""
    t = (text or "").strip()
    if _FORGET_ALL_RE.match(t):
        return ("all", None)
    m = _FORGET_RE.match(t)
    if not m:
        return None
    q = m.group(2).strip()
    return ("query", q) if q else ("latest", None)
