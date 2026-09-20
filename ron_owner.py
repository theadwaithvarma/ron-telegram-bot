#!/usr/bin/env python3
"""Owner-only power commands for the Ron Telegram bot (v1.12.0).

Self-contained logic + state for the owner's command deck: /stats, /ban,
/unban, /broadcast, /say, /restart, /quiet, /logs, /users, /forgetuser,
/model (and the /active ledger's ephemeral replies).

Transport (sending/deleting Telegram messages) stays in ron_daemon.py —
this module builds reply texts and owns its small JSON state files, so
it's unit-testable with no network and no credentials.

State files (all in the daemon dir, atomic tmp+rename writes):
  ron_stats.json        per-day traffic counters {in, out, chats, ai calls}
  ron_users.json        user registry {name, username, first/last seen, last chat}
  ron_known_chats.json  chat registry {title, type, last_seen} (for /broadcast)
  ron_banned.json       banned user ids (write-through on change)

All in-memory state is guarded by one RLock; stats/users/chats flush to
disk every 60s via start_flusher() (bans flush immediately on change).
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

# How long an owner-command reply (and the command message that triggered
# it) stays visible before both are auto-deleted. Module-level so it's
# easy to tune; set to 0 to disable auto-deletion (used by tests).
EPHEMERAL_TTL = 30

# Two-step /broadcast: how long the owner has to confirm.
BROADCAST_WINDOW = 60

IST = timezone(timedelta(hours=5, minutes=30))

_DIR = None
_LOCK = threading.RLock()

# In-memory accumulators (flushed periodically).
_stats = {}   # day -> {"date", "in", "out", "chats": {chat_id: n}, "ai": {provider: n}}
_users = {}   # user_id -> {"name","username","first_seen","last_seen","last_chat","chat_type"}
_chats = {}   # chat_id -> {"title","type","last_seen"}
_bans = {}    # user_id -> {"ts","by"}

# Single pending broadcast: {"text","chats","ts","by"}.
_BROADCAST = {}


# ---------------------------------------------------------------------------
# State-file plumbing
# ---------------------------------------------------------------------------
def set_dir(path):
    """Point state files at the daemon dir; load existing state."""
    global _DIR
    _DIR = path
    if not path:
        return
    with _LOCK:
        _stats.update(_load("ron_stats.json", {}).get("days", {}))
        _users.update(_load("ron_users.json", {}))
        _chats.update(_load("ron_known_chats.json", {}))
        _bans.update(_load("ron_banned.json", {}).get("banned", {}))


def _path(name):
    return os.path.join(_DIR, name) if _DIR else None


def _load(name, default):
    p = _path(name)
    if not p:
        return default
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def _save(name, data):
    p = _path(name)
    if not p:
        return
    try:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        pass


def flush():
    """Persist stats, users, chats. Best-effort — never raises."""
    try:
        with _LOCK:
            _prune_days_locked()
            _save("ron_stats.json", {"days": _stats})
            _save("ron_users.json", _users)
            _save("ron_known_chats.json", _chats)
    except Exception:
        pass


def start_flusher():
    """Background thread: flush state files every 60s. Daemon-owned."""

    def _loop():
        while True:
            time.sleep(60)
            flush()

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Traffic counters (/stats)
# ---------------------------------------------------------------------------
def day_key(now=None):
    if now is None:
        return datetime.now(IST).strftime("%Y-%m-%d")
    return datetime.fromtimestamp(now, IST).strftime("%Y-%m-%d")


def _day_entry_locked(day):
    e = _stats.get(day)
    if e is None:
        e = {"date": day, "in": 0, "out": 0, "chats": {}, "ai": {}}
        _stats[day] = e
    return e


def _prune_days_locked():
    """Keep 30 days of counters; the files stay small forever."""
    cutoff = (datetime.now(IST) - timedelta(days=30)).strftime("%Y-%m-%d")
    for d in [d for d in _stats if d < cutoff]:
        del _stats[d]


def note_in(chat_id):
    """One inbound message seen. Cheap: dict increments under a lock."""
    with _LOCK:
        _day_entry_locked(day_key())["in"] += 1


def note_out():
    """One outbound message sent (called from the daemon's send())."""
    with _LOCK:
        _day_entry_locked(day_key())["out"] += 1


def note_ai_call(provider):
    """One /chat/completions POST attempt to a provider (ron_ai hook)."""
    if not provider:
        return
    with _LOCK:
        ai = _day_entry_locked(day_key())["ai"]
        ai[str(provider)] = ai.get(str(provider), 0) + 1


def today_snapshot():
    """Copy of today's counters for /stats (never the live dict)."""
    with _LOCK:
        e = _day_entry_locked(day_key())
        return {"date": e["date"], "in": e["in"], "out": e["out"],
                "chats": dict(e["chats"]),
                "ai": dict(e["ai"])}


def stats_text(day, cache_stats, uptime):
    """Pure builder: the /stats card."""
    ai = day.get("ai", {})
    ai_s = " · ".join(f"{k}: {v}" for k, v in sorted(ai.items())) or "—"
    hits = int((cache_stats or {}).get("hits", 0) or 0)
    misses = int((cache_stats or {}).get("misses", 0) or 0)
    tot = hits + misses
    rate = f"{100 * hits // tot}%" if tot else "—"
    return (
        f"\U0001F4CA Today ({day.get('date', '')})\n"
        f"\u2022 in: {day.get('in', 0)} \u00b7 out: {day.get('out', 0)}\n"
        f"\u2022 active chats: {len(day.get('chats', {}))}\n"
        f"\u2022 AI calls \u2014 {ai_s}\n"
        f"\u2022 cache: {rate} hit ({hits}/{tot})\n"
        f"\u2022 uptime: {uptime}"
    )


# ---------------------------------------------------------------------------
# User + chat registries (/users, /broadcast targets)
# ---------------------------------------------------------------------------
def note_traffic(user_id, first_name, username, chat_id, chat_title,
                 chat_type):
    """One handled update: inbound counter + user registry + chat registry.

    Metadata only — no message content is ever stored here.
    """
    if not user_id or not chat_id:
        return
    now = time.time()
    uid, cid = str(user_id), str(chat_id)
    name = (first_name or "there").strip() or "there"
    with _LOCK:
        day = _day_entry_locked(day_key())
        day["in"] += 1
        day["chats"][cid] = day["chats"].get(cid, 0) + 1
        u = _users.get(uid)
        if u is None:
            _users[uid] = {
                "name": name,
                "username": (username or "").strip(),
                "first_seen": now,
                "last_seen": now,
                "last_chat": chat_title or "DM",
                "chat_type": chat_type or "private",
            }
        else:
            u["name"] = name
            u["username"] = (username or "").strip()
            u["last_seen"] = now
            u["last_chat"] = chat_title or "DM"
            u["chat_type"] = chat_type or "private"
        _chats[cid] = {
            "title": chat_title or ("DM" if chat_type == "private" else "group"),
            "type": chat_type or "private",
            "last_seen": now,
        }
        if len(_users) > 10000:
            # Bound the registry: drop the stalest entries.
            for stale in sorted(_users, key=lambda k: _users[k]["last_seen"])[:1000]:
                del _users[stale]


def user_roster():
    """Registry entries, most-recently-seen first."""
    with _LOCK:
        return sorted(_users.values(), key=lambda u: u.get("last_seen", 0),
                      reverse=True)


def drop_user(user_id):
    """Forget one user's registry entry (/forgetuser)."""
    with _LOCK:
        _users.pop(str(user_id), None)


def _ago(ts):
    s = int(time.time() - ts)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _datestr(ts):
    try:
        return datetime.fromtimestamp(ts, IST).strftime("%b %d")
    except Exception:
        return "?"


def users_text(roster, limit=30):
    """Pure builder: the /users card."""
    if not roster:
        return "No users seen yet."
    lines = [f"\U0001F465 Users ({len(roster)} total)"]
    for u in roster[:limit]:
        uname = f" (@{u['username']})" if u.get("username") else ""
        lines.append(
            f"\u2022 {u.get('name', '?')}{uname} \u2014 "
            f"{u.get('last_chat', '?')} \u00b7 {_ago(u.get('last_seen', 0))} "
            f"\u00b7 since {_datestr(u.get('first_seen', 0))}")
    if len(roster) > limit:
        lines.append(f"\u2026 and {len(roster) - limit} more")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bans (/ban, /unban)
# ---------------------------------------------------------------------------
def is_banned(user_id):
    if not user_id:
        return False
    with _LOCK:
        return str(user_id) in _bans


def ban(user_id, by):
    with _LOCK:
        _bans[str(user_id)] = {"ts": time.time(), "by": by}
        _save("ron_banned.json", {"banned": _bans})


def unban(user_id):
    with _LOCK:
        removed = _bans.pop(str(user_id), None) is not None
        if removed:
            _save("ron_banned.json", {"banned": _bans})
        return removed


def banned_list():
    with _LOCK:
        return sorted(_bans)


# ---------------------------------------------------------------------------
# Broadcast (/broadcast — two-step confirm)
# ---------------------------------------------------------------------------
def broadcast_request(text, by):
    """Stage a broadcast. Returns (chat_count, preview)."""
    with _LOCK:
        chats = sorted(_chats)
        if not chats:
            return 0, ""
        preview = text[:120] + ("\u2026" if len(text) > 120 else "")
        _BROADCAST.clear()
        _BROADCAST.update({"text": text, "chats": chats,
                           "ts": time.time(), "by": by})
        return len(chats), preview


def broadcast_confirm(by):
    """Confirm a staged broadcast. Returns ("ok", text, chats) or
    ("error", message)."""
    with _LOCK:
        if not _BROADCAST:
            return ("error", "Nothing pending \u2014 send /broadcast <text> first.")
        if _BROADCAST.get("by") != by:
            return ("error", "That broadcast wasn't yours.")
        if time.time() - _BROADCAST.get("ts", 0) > BROADCAST_WINDOW:
            _BROADCAST.clear()
            return ("error", "Broadcast expired \u2014 send it again.")
        text, chats = _BROADCAST["text"], list(_BROADCAST["chats"])
        _BROADCAST.clear()
        return ("ok", text, chats)


def broadcast_cancel(by):
    with _LOCK:
        if _BROADCAST and _BROADCAST.get("by") == by:
            _BROADCAST.clear()
            return True
        return False


# ---------------------------------------------------------------------------
# Restart (/restart)
# ---------------------------------------------------------------------------
def restart_flag_path():
    return os.path.join(_DIR, "ron_restart.requested") if _DIR else None


def request_restart():
    """Drop the flag file the watchdog honors for a clean, crash-free
    restart. Raises if the flag can't be written."""
    p = restart_flag_path()
    if not p:
        raise RuntimeError("owner state dir not set")
    with open(p, "w") as f:
        f.write(str(int(time.time())))


# ---------------------------------------------------------------------------
# Quiet (/quiet)
# ---------------------------------------------------------------------------
def parse_quiet_minutes(args):
    """'30' -> 30. Raises ValueError on anything outside 1..1440."""
    try:
        minutes = int((args or "").strip())
    except (ValueError, AttributeError):
        raise ValueError("Usage: /quiet <minutes> (1\u20131440)")
    if not 1 <= minutes <= 1440:
        raise ValueError("Usage: /quiet <minutes> (1\u20131440)")
    return minutes


def format_ist(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%H:%M")


def quiet_text(minutes, until):
    return (f"\U0001F507 Quiet for {minutes} min \u2014 auto-resuming "
            f"{format_ist(until)} IST. /start wakes me early.")


# ---------------------------------------------------------------------------
# Logs (/logs)
# ---------------------------------------------------------------------------
def tail_log(path, n):
    """Last n lines of a log file. Pure file op, no subprocess."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        return lines[-n:] if n else []
    except Exception:
        return []


def logs_text(lines):
    if not lines:
        return "Log is empty."
    body = "\n".join(lines)
    if len(body) > 3800:
        body = "\u2026\n" + body[-3800:]
    return f"```\n{body}\n```"


# ---------------------------------------------------------------------------
# Forget user (/forgetuser)
# ---------------------------------------------------------------------------
def forget_user(user_id, mem):
    """Wipe one user's attributable data.

    Registry entry is always dropped. Memory-DB rows go through the
    store's wipe_user() when the store is available (None when the
    cryptography dep is missing). Returns the counts dict, or None when
    there was no memory store.
    """
    drop_user(user_id)
    if mem is None:
        return None
    return mem.wipe_user(int(user_id))


def forgetuser_text(user_id, counts, purged_turns):
    if counts is None:
        base = (f"\U0001F9F9 Forgot user {user_id}: memory DB unavailable "
                f"\u2014 registry entry + history purged only.")
    else:
        base = (
            f"\U0001F9F9 Forgot user {user_id}:\n"
            f"\u2022 memory: {counts.get('messages', 0)} msgs, "
            f"{counts.get('facts', 0)} facts, "
            f"{counts.get('consents', 0)} consents, "
            f"{counts.get('pendings', 0)} pendings, "
            f"{counts.get('members', 0)} member rows, "
            f"{counts.get('meta', 0)} meta rows\n"
            f"\u2022 history turns purged: {purged_turns}")
    return (base + "\nTheir DM chat is fully wiped (facts, summaries, bot "
                   "replies); other chats' facts untouched.")


# ---------------------------------------------------------------------------
# Model chain (/model)
# ---------------------------------------------------------------------------
_PINNABLE = ("groq", "gemini", "openrouter")


def apply_pin(cfg, name):
    """Pure: providers list with `name` moved first (stable for the rest).

    Raises ValueError with a usage message on bad input.
    """
    name = (name or "").strip().lower()
    if name not in _PINNABLE:
        raise ValueError(f"Usage: /model {'|'.join(_PINNABLE)}")
    providers = [dict(p) for p in (cfg.get("providers") or [])]
    if not any(str(p.get("name", "")).lower() == name for p in providers):
        raise ValueError(f"{name} isn't in the chain right now.")
    providers.sort(key=lambda p: 0
                   if str(p.get("name", "")).lower() == name else 1)
    return providers


def model_text(chain_info):
    """Pure builder: the /model card. chain_info = list of
    {"name","models","status","primary"}."""
    lines = ["\U0001F9E0 Provider chain:"]
    for p in chain_info:
        tag = " \u2190 primary" if p.get("primary") else ""
        models = ", ".join(p.get("models") or [])
        lines.append(f"\u2022 {p.get('name')} ({models}) \u2014 "
                     f"{p.get('status', '?')}{tag}")
    lines.append("/model <groq|gemini|openrouter> to pin one first.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Ephemeral replies
# ---------------------------------------------------------------------------
def deletion_targets(send_result, cmd_msg_id):
    """Message ids to auto-delete: the bot's reply + the command message."""
    ids = []
    try:
        mid = (send_result.get("result") or {}).get("message_id")
    except Exception:
        mid = None
    if mid:
        ids.append(mid)
    if cmd_msg_id:
        ids.append(cmd_msg_id)
    return ids


def parse_user_id(args):
    """First token as a positive int user id, else None."""
    try:
        uid = int((args or "").split(None, 1)[0])
        return uid if uid > 0 else None
    except (ValueError, IndexError):
        return None
