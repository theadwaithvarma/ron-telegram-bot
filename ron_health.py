#!/usr/bin/env python3
"""Phase 7 (2026-09-20) — /health status card.

Pure functions only: no daemon import, no network, no secrets. The daemon
assembles an `info` dict from its own globals + on-disk JSON and calls
build_health_card(info). Every field is optional — anything missing or
broken renders as "n/a" instead of raising, because a status command must
never fail.

Privacy: provider names (groq/gemini/openrouter) and model names NEVER
appear here — Adwaith's rule is that other users must not know what's
powering the bot. Providers are labeled by chain position only
(primary / fallback-1 / fallback-2). No key material, no memory content,
no user ids.
"""
import time

# Provider names that must never leak into user-visible output.
_PROVIDER_NAMES = ("groq", "gemini", "openrouter")


def _age(ts, now):
    """Human age of an epoch timestamp: '45s', '6m', '3h', '2d'. 'n/a'."""
    if not ts:
        return "n/a"
    return _dur(max(0, now - ts))


def _dur(secs):
    """Human duration for a span in seconds: '45s', '6m', '3h', '2d'."""
    s = max(0, int(secs))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _uptime(secs):
    if secs is None or secs < 0:
        return "n/a"
    s = int(secs)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


def _provider_line(providers):
    """One compact line for the AI chain.

    providers: list of (label, remaining_secs|None, state) where state is
    one of ok / cooling / no_key / down / unknown. Labels are chain
    positions only — never real provider names.
    """
    if not providers:
        return "n/a"
    parts = []
    for label, state, extra in providers:
        if state == "ok":
            parts.append(f"{label} ✓")
        elif state == "cooling":
            parts.append(f"{label} cooling"
                         + (f" {_dur(extra)} left" if extra else ""))
        elif state == "no_key":
            parts.append(f"{label} no key")
        elif state == "down":
            parts.append(f"{label} down")
        else:
            parts.append(f"{label} …")
    return " · ".join(parts)


def _tokens(n):
    if n is None:
        return "n/a"
    n = int(n)
    if n >= 1000:
        return f"~{n / 1000:.1f}k"
    return str(n)


def build_health_card(info):
    """Build the compact plain-text status card from an info dict.

    All keys optional:
      start_ts (epoch), degraded (bool), providers (list, see
      _provider_line), memory_on (bool), summaries (int|None),
      cache_hits/cache_misses/cache_tokens (ints), backup_ts (epoch|None),
      restarts (int|None).
    """
    info = info or {}
    now = time.time()
    lines = ["🏥 Ron health"]

    up = _uptime(now - info["start_ts"]) if info.get("start_ts") else "n/a"
    brain = "degraded ⚠️" if info.get("degraded") else "ok"
    lines.append(f"⏱ up {up} · brain {brain}")

    lines.append("🧠 " + _provider_line(info.get("providers")))

    if info.get("memory_on"):
        s = info.get("summaries")
        mem_line = f"💾 memory on" + (f" · {s} chats summarized"
                                     if s is not None else "")
    else:
        mem_line = "💾 memory off"
    lines.append(mem_line)

    h, m = info.get("cache_hits"), info.get("cache_misses")
    if h is not None and m is not None and (h + m) > 0:
        rate = int(round(100.0 * h / (h + m)))
        cache_line = (f"⚡ cache {rate}% hit"
                      f" · {_tokens(info.get('cache_tokens'))} tokens saved")
    else:
        cache_line = "⚡ cache n/a"
    lines.append(cache_line)

    n = info.get("images_24h")
    g = info.get("images_gen_24h")
    ns = n if n is not None else "n/a"
    gs = g if g is not None else "n/a"
    lines.append(f"🖼 images 24h: read {ns} · generated {gs}")

    bts = info.get("backup_ts")
    # Flag a backup pipeline that is failing: no good snapshot in 36h
    # while runs keep erroring. Surfaces in /health so a dead 3am cron
    # is visible instead of silently absent.
    failing = bool(info.get("backup_error")) and (
        not bts or (now - bts) > 36 * 3600)
    if bts:
        bl = f"💿 backup {_age(bts, now)} ago ✓"
    else:
        bl = "💿 backup none yet"
    lines.append(bl + (" ⚠️ failing" if failing else ""))

    r = info.get("restarts")
    lines.append(f"🐶 watchdog restarts: {r if r is not None else 'n/a'}")

    card = "\n".join(lines)
    # Belt and braces: the card must never name providers or leak anything
    # that looks like a key, even if a caller passes a bad label.
    low = card.lower()
    for name in _PROVIDER_NAMES:
        if name in low:
            raise ValueError("health card leaked a provider name")
    return card
