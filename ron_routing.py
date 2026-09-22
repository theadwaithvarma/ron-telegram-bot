#!/usr/bin/env python3
"""Difficulty-based routing for the Ron Telegram bot (Track 3).

Deterministic, zero-LLM-call difficulty classifier plus a tier-aware
model-ordering plan builder. This module is deliberately independent of
ron_ai.py (no import) so it can be unit-tested standalone and wired in
without touching the existing failover machinery. (The referenced
upgrade-specs/track3-integration.md was never written; the integration
contract — exact function names/signatures and ron_ai.py anchor points —
is defined in the Track C final report instead.)

Tiers:
    "cheap"    — greetings / small-talk / thanks. Smallest model first,
                 short system prompt, memory recall skipped. Extends the
                 spirit of ron_ai._looks_trivial (which this must stay
                 consistent with).
    "standard" — everything else. Current default ordering.
    "hard"     — code / stack traces, math, why/how explanations,
                 multi-part or long questions, debugging. Strongest model
                 first, full prompt, memory recall on.

Routing semantics preserved from ron_ai (the wiring in ron_ai.py must
keep these — this module only reorders, never drops):
    * full cross-provider failover on every tier — reordering never
      removes a (provider, model) pair from the plan;
    * per-provider 429 cooldowns / instant failover (v1.26.1 speed fix);
    * 5xx circuit breaker;
    * zero-cost canned acks for pure ok/lol/thanks (daemon-level, before
      the brain is called — the classifier still labels them "cheap"
      for consistency);
    * no double-burn after full chain exhaustion.
"""

import re

# ----------------------------------------------------------------------
# Tier vocabulary
# ----------------------------------------------------------------------

CHEAP = "cheap"
STANDARD = "standard"
HARD = "hard"
TIERS = (CHEAP, STANDARD, HARD)

# ----------------------------------------------------------------------
# Model maps (mirror ron_ai.py — keep in sync when the provider roster
# changes; see DEFAULT_PROVIDERS / _CHEAP_MODELS there)
# ----------------------------------------------------------------------

# Smallest model per provider: tried first for the "cheap" tier.
_CHEAP_MODELS = {
    "groq": "openai/gpt-oss-20b",
    "gemini": "gemini-flash-lite-latest",
    "openrouter": "nex-agi/nex-n2.5-pro:free",
}

# Strongest model per provider: tried first for the "hard" tier.
_STRONGEST_MODELS = {
    "groq": "openai/gpt-oss-120b",
    "gemini": "gemini-3.8-flash",
    "openrouter": "openai/gpt-oss-120b:free",
}

# ----------------------------------------------------------------------
# "cheap" signals — whole-message greetings / small-talk / thanks
# ----------------------------------------------------------------------

# Mirrors ron_ai._TRIVIAL_RE's spirit, extended with thanks/acks. Must
# match the WHOLE stripped message (anchored both ends): "hey, what's
# 2+2?" starts with a greeting but is a real question.
_CHEAP_GREETING_RE = re.compile(
    r"^(hi+|hello+|hey+|yo|sup|howdy|namaste|vanakkam|"
    r"how are you|how r u|hows it going|how's it going|whats up|what's up|"
    r"who are you|who r u|"
    r"good morning|good afternoon|good evening|good night)"
    r"\b[.!?~]*$", re.IGNORECASE)

# Bare acknowledgements the daemon already answers with a zero-cost
# canned ack — labeled "cheap" here for wiring consistency.
_CHEAP_ACKS = frozenset({
    "ok", "okay", "k", "kk", "thanks", "thank you", "thx", "ty", "tyvm",
    "lol", "lmao", "lmaoo", "haha", "nice", "cool", "great", "awesome",
    "👍", "🙏", "👌", "❤️", "😂",
})

# ----------------------------------------------------------------------
# "hard" signals
# ----------------------------------------------------------------------

# Fenced or indented code blocks, and stack traces / tracebacks.
_HARD_CODE_RE = re.compile(r"```|~~~")
_HARD_TRACE_RE = re.compile(
    r"(?im)("
    r"^traceback \(most recent call last\)"
    r"|^file \"[^\"]+\", line \d+"
    r"|^\s*at [\w.$<>]+\([^)]*:\d+\)"      # java/c# frames
    r"|^\s*at [\w.$]+\s*\("                # node frames
    r"|exception in thread"
    r"|\b\w*(error|exception|panic):"      # TypeError: ... / panic: ...
    r")")

# Math: digits combined with arithmetic operators or "=", or explicit
# calculation/solve keywords. Kept deliberately trigger-happy — a
# wrong-answer on arithmetic is worse than a slightly slower reply.
_HARD_MATH_RE = re.compile(
    r"(?<!\w)\d[\d\s.,]*[+\-*/^%×÷=][\d\s(]"
    r"|=\s*\d"
    r"|\b(solve|derivative|integral|differentiate|integrate|simplify|"
    r"factor(ize|ise)?|calculate|compute|evaluat(e|ion)|"
    r"what'?s \d|what is \d)\b",
    re.IGNORECASE)

# Explanations of mechanism/cause, and debugging requests.
_HARD_WHY_HOW_RE = re.compile(
    r"\b(explain\s+(why|how)"
    r"|why\s+(does|do|is|are|did|would|should|can|could|won't|can't|doesn't)"
    r"|how\s+(do|does|can|would|should|could|to))\b",
    re.IGNORECASE)
_HARD_DEBUG_RE = re.compile(
    r"\b(debug|debugging|traceback|stack ?trace"
    r"|fix\s+(this|the|my|that)"
    r"|what'?s wrong with"
    r"|why\s+(isn'?t|aren'?t|doesn'?t|don'?t|won'?t|can'?t)\b.*?\b"
    r"(working|compiling|running|loading|rendering|responding)"
    r"|not\s+(working|compiling|running|loading|responding)"
    r"|bug\s+in|error\s+in)\b",
    re.IGNORECASE)

# Multi-part: 2+ questions, or a numbered/bulleted list of asks.
_HARD_MULTIPART_RE = re.compile(r"(?m)^\s*(?:\d+[.)]|[-*•])\s+\S")

# Length/complexity floors. 600 chars ≈ a paragraph-plus ask that
# benefits from the strongest model; 5+ sentences ≈ a compound ask.
_HARD_LONG_CHARS = 600
_HARD_MANY_SENTENCES = 5
_SENTENCE_END_RE = re.compile(r"[.!?]+(?:\s|$)")

# How-to phrasing that implies a real task, not chit-chat.
_HARD_TASK_RE = re.compile(
    r"\b(how\s+(do|can)\s+i\s+|walk\s*me\s+through|step[\s-]*by[\s-]*step|"
    r"pros?\s+and\s+cons?|compare\s+\w+\s+(vs|versus|with)|"
    r"which\s+is\s+better|should\s+i\s+(use|buy|choose|learn))\b",
    re.IGNORECASE)


def _looks_cheap(text):
    """True for whole-message greetings, small-talk, thanks, bare acks."""
    t = (text or "").strip()
    if not t or len(t) > 80:
        return False
    bare = re.sub(r"[.!?~\s]+$", "", t.lower())
    if bare in _CHEAP_ACKS:
        return True
    return bool(_CHEAP_GREETING_RE.match(t))


def classify_difficulty(text):
    """Classify a user message: "cheap" | "standard" | "hard".

    Pure heuristics, zero LLM calls, never raises. Cheap is checked
    first so a greeting-shaped message never accidentally lands hard;
    then hard signals; everything else is standard.
    """
    t = (text or "").strip()
    if _looks_cheap(t):
        return CHEAP
    if not t:
        return STANDARD
    if (_HARD_CODE_RE.search(t)
            or _HARD_TRACE_RE.search(t)
            or _HARD_MATH_RE.search(t)
            or _HARD_WHY_HOW_RE.search(t)
            or _HARD_DEBUG_RE.search(t)
            or _HARD_TASK_RE.search(t)
            or t.count("?") >= 2
            or len(_HARD_MULTIPART_RE.findall(t)) >= 2
            or len(t) >= _HARD_LONG_CHARS
            or len(_SENTENCE_END_RE.findall(t)) >= _HARD_MANY_SENTENCES):
        return HARD
    return STANDARD


def routing_tier(user_text, cfg):
    """Effective routing tier for one message, honoring light mode.

    Light mode forces "cheap" for every message (same semantics as
    ron_ai._use_cheap: "running lean"), regardless of classification.
    cfg may be None/{} — then it's pure classification.
    """
    if cfg and cfg.get("light_mode"):
        return CHEAP
    return classify_difficulty(user_text)


def use_short_prompt(tier):
    """True when the tier gets the short system prompt + no recall.

    Mirrors ron_ai's cheap handling: only the cheap tier runs the
    ~270-char system_cheap prompt and skips memory recall. Hard and
    standard both need the full prompt (and recall) for quality.
    """
    return tier == CHEAP


def order_provider_models(name, models, tier):
    """Reorder one provider's model list for a tier. Pure, never drops.

    "cheap"    -> provider's smallest model first (existing _request
                behavior, kept identical);
    "hard"     -> provider's strongest model first;
    "standard" -> unchanged (current default ordering).
    The rest of the provider's models stay behind in original order so
    429/5xx failover walks the full chain either way.
    """
    models = list(models or [])
    if tier == CHEAP:
        first = _CHEAP_MODELS.get(name)
    elif tier == HARD:
        first = _STRONGEST_MODELS.get(name)
    else:
        return models
    if first in models:
        models = [first] + [m for m in models if m != first]
    return models


def build_tiered_plan(providers, tier):
    """Build the ordered (provider_name, model) attempt plan for a tier.

    providers: list of {"name": ..., "models": [...]} dicts (same shape
    as ron_ai's cfg["providers"]). Applies order_provider_models per
    provider and concatenates provider order unchanged. Raises
    ValueError on an unknown tier.

    This is the plan-builder the routing tests mock/probe: it proves
    hard puts the strongest model first, cheap puts the smallest first,
    and — critically — that every (provider, model) pair survives in
    the plan so failover still walks the whole chain.
    """
    if tier not in TIERS:
        raise ValueError(f"unknown routing tier: {tier!r}")
    plan = []
    for p in providers or []:
        name = p.get("name")
        for m in order_provider_models(name, p.get("models"), tier):
            plan.append((name, m))
    return plan
