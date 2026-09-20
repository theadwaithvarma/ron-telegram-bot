#!/usr/bin/env python3
"""Ron AI daemon — conversational Telegram bot, long-polling getUpdates.

Replies to everyone: DMs from any user, and group messages that mention
the bot or reply to it. (Owner-only pause was lifted 2026-09-20.)
Replies use ron_ai.py (Groq backend), streaming live via rich drafts for
long 1:1 answers, sent as native rich messages (Bot API 10.1
sendRichMessage) with contextual quick-reply buttons.
Business / Secretary Mode: handles business_connection updates and answers
business_message updates on the owner's business account.

Run: nohup python3 ron_daemon.py >/tmp/ron_daemon.log 2>&1 &
State: offset persisted to ~/workspace/telegram-bot/ron_daemon.offset
"""
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.request
import urllib.parse

import http_pool

sys.path.insert(0, os.path.join(os.path.expanduser("~"), "workspace", "telegram-bot"))
import ron_ai
import rich_send as rich_mod
# Phase 6: semantic answer cache. None-safe — the bot replies normally
# (just without caching) if the module ever fails to import.
try:
    import ron_cache
except Exception:
    ron_cache = None
# Phase 7: /health status card builders (pure functions, no daemon import).
# None-safe like ron_cache — /health degrades to "n/a" without it.
try:
    import ron_health
except Exception:
    ron_health = None
# Rich-message blocks builder (Bot API 10.3): markdown -> blocks form so
# buttons render INSIDE the bubble. None-safe — without it, button replies
# fall back to the classic under-bubble keyboard.
try:
    import ron_richblocks
except Exception:
    ron_richblocks = None

# Image reading (photos -> Gemini vision). None-safe — without it the bot
# tells users photos aren't supported instead of crashing.
try:
    import ron_vision
except Exception:
    ron_vision = None

# Image generation (/imagine -> Pollinations). None-safe — without it the
# bot tells users generation isn't supported instead of crashing.
try:
    import ron_imagen
except Exception:
    ron_imagen = None

# Per-command quotas (/factcheck, /think). None-safe — without it the
# commands refuse politely instead of crashing.
try:
    import ron_cmdquota
except Exception:
    ron_cmdquota = None

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
from dynamic_credentials import dynamic_credential_entry

HOME = os.path.expanduser("~")
OFFSET_FILE = os.path.join(HOME, "workspace", "telegram-bot", "ron_daemon.offset")
OWNER_ID = 6819786239  # Adwaith — bot owner (no longer a reply gate)
PLAYGROUND = "-1003920795086"

# Maintenance mode: /shutdown (owner only) makes the bot go quiet for
# everyone except the owner; /start (owner) brings it back. Persisted to
# disk so it survives daemon restarts.
MAINT_FILE = os.path.join(HOME, "workspace", "telegram-bot",
                           "ron_maintenance.json")


def maintenance_on():
    """True when the bot is in owner-only maintenance mode."""
    try:
        with open(MAINT_FILE) as f:
            return bool(json.load(f).get("maintenance"))
    except (OSError, ValueError):
        return False


def set_maintenance(on, by):
    """Persist maintenance mode. Atomic write like the other state files."""
    tmp = MAINT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"maintenance": bool(on), "by": by, "ts": time.time()}, f)
    os.replace(tmp, MAINT_FILE)

_entry = dynamic_credential_entry("custom.telegram")
_SURROGATE = str(_entry["surrogate"]).strip()

# Graceful shutdown: restarts must not eat in-flight replies. The poll loop
# saves the update offset as soon as a reply thread is spawned, so killing
# the process mid-reply would ack a message that never got answered.
# Instead, SIGTERM/SIGINT stops the loop and drains in-flight reply threads.
_SHUTDOWN = threading.Event()
_INFLIGHT = set()
_INFLIGHT_LOCK = threading.Lock()
# Guards read-modify-write of the small state JSON files (the poll loop and
# reply threads both touch known_topics.json).
_FILE_LOCK = threading.RLock()
# Phase 7: daemon start time for /health uptime. Set once at import.
_START_TS = time.time()
# Phase 7: last provider probe results, refreshed by the startup health
# ping and the degraded-mode recovery loop. /health reads this — no live
# probing on the status path, so the card is instant and free.
_LAST_PROBE = {}
_PROBE_LOCK = threading.Lock()


def _record_probe_results(results):
    """Best-effort stash of probe_providers() output for /health."""
    try:
        with _PROBE_LOCK:
            _LAST_PROBE.clear()
            _LAST_PROBE.update(results)
    except Exception:
        pass


def _atomic_write_json(path, obj):
    """Write JSON via tmp+rename so a crash mid-write never leaves a torn file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


# --- Phase 3: watchdog hooks + degraded mode (2026-09-20) ---
# The watchdog (ron_watchdog.py) restarts this daemon when it dies; the
# daemon writes a heartbeat every 30s so a hung process is detectable too.
# Degraded mode: if every AI provider keeps failing, the bot stops burning
# API calls, tells the user honestly, queues their message to disk, and
# answers it when a provider recovers — no silent drops, no duplicates.
HEARTBEAT_FILE = os.path.join(HOME, "workspace", "telegram-bot",
                              "ron_heartbeat.json")
DEGRADED_QUEUE = os.path.join(HOME, "workspace", "telegram-bot",
                              "degraded_queue.jsonl")
BRAIN_FAIL_WINDOW = 600.0    # systemic failures counted over the last 10 min
BRAIN_FAIL_THRESHOLD = 5     # this many -> degraded mode
RECOVERY_POLL_SECS = 60      # provider probe interval while degraded
QUEUE_MAX_AGE = 1800         # don't answer queued messages older than 30 min
DEGRADED_NOTICE_COOLDOWN = 900.0  # one honest notice per chat per 15 min
_SYSTEMIC_ERR_MARKERS = ("rate limited", "not connected yet",
                         "AI backend error", "AI stream stalled",
                         "AI stream closed", "AI backend returned empty reply")

_BRAIN_LOCK = threading.Lock()
_brain_failures = []         # timestamps of recent systemic AI failures
_degraded = {"active": False, "since": 0.0}
_degraded_notice_ts = {}     # chat_id -> last honest-notice timestamp
_QUEUE_LOCK = threading.Lock()
_TAP_LOCK = threading.Lock()
_tap_ts = {}                 # (chat_id, user_id, data) -> last tap time
_TAP_WINDOW = 30             # button-mash throttle window (seconds)


def _heartbeat_loop():
    """Write pid+timestamp every 30s so the watchdog can spot a hang."""
    while not _SHUTDOWN.is_set():
        try:
            _atomic_write_json(HEARTBEAT_FILE,
                               {"pid": os.getpid(), "ts": time.time()})
        except Exception:
            pass
        for _ in range(60):  # 0.5s granularity so shutdown is prompt
            if _SHUTDOWN.is_set():
                break
            time.sleep(0.5)


def _brain_down():
    with _BRAIN_LOCK:
        return _degraded["active"]


def _record_brain_failure(err_text):
    """Count one systemic AI failure; flip to degraded mode at threshold."""
    with _BRAIN_LOCK:
        now = time.time()
        _brain_failures.append(now)
        cutoff = now - BRAIN_FAIL_WINDOW
        while _brain_failures and _brain_failures[0] < cutoff:
            _brain_failures.pop(0)
        if (not _degraded["active"]
                and len(_brain_failures) >= BRAIN_FAIL_THRESHOLD):
            _degraded["active"] = True
            _degraded["since"] = now
            print(f"[ron_daemon] DEGRADED MODE ON: {len(_brain_failures)} "
                  f"systemic AI failures in 10 min", flush=True)


def _record_brain_success():
    """A real answer got through — clear the failure streak."""
    with _BRAIN_LOCK:
        if _brain_failures:
            _brain_failures.clear()
        if _degraded["active"]:
            _degraded["active"] = False
            print("[ron_daemon] DEGRADED MODE OFF: provider recovered",
                  flush=True)


def _is_systemic(err_text):
    return any(m in (err_text or "") for m in _SYSTEMIC_ERR_MARKERS)


def _queue_message(chat_id, user_id, user_name, text, reply_to=None,
                   thread_id=None, business_connection_id=None, private=True):
    """Persist one unanswered message to the degraded-mode queue."""
    ts = time.time()
    entry = {"qid": f"{ts:.6f}:{chat_id}:{user_id}", "ts": ts,
             "chat_id": chat_id, "user_id": user_id, "user_name": user_name,
             "text": text, "reply_to": reply_to, "thread_id": thread_id,
             "business_connection_id": business_connection_id,
             "private": private}
    with _QUEUE_LOCK:
        try:
            # Dedupe: the same (chat, user, text) already queued
            # (button-mash, double-sends) must not pile up — one queued
            # copy is answered on recovery, the rest would just be spam.
            try:
                with open(DEGRADED_QUEUE) as f:
                    for line in f:
                        try:
                            e = json.loads(line)
                        except ValueError:
                            continue
                        if (str(e.get("chat_id")) == str(chat_id)
                                and str(e.get("user_id")) == str(user_id)
                                and (e.get("text") or "") == (text or "")):
                            print("[ron_daemon] queue dedupe: already queued "
                                  f"(chat={chat_id})", flush=True)
                            return True
            except FileNotFoundError:
                pass
            with open(DEGRADED_QUEUE, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[ron_daemon] queue write failed: {e}", flush=True)
            return False
    print(f"[ron_daemon] queued message for chat={chat_id} (degraded)",
          flush=True)
    return True


def _maybe_degraded_notice(chat_id, reply_to=None, thread_id=None,
                           business_connection_id=None):
    """Honest, provider-neutral 'brain is down, you're queued' message —
    at most one per chat per 15 minutes."""
    now = time.time()
    with _BRAIN_LOCK:
        last = _degraded_notice_ts.get(str(chat_id), 0)
        if now - last < DEGRADED_NOTICE_COOLDOWN:
            return
        _degraded_notice_ts[str(chat_id)] = now
    notice = ("Whoa \u2014 every AI backend I use is down at the same time. "
              "I've queued your message and I'll answer it as soon as one "
              "comes back. No need to resend. \U0001F527")
    try:
        send_rich_with_kb(chat_id, notice, reply_to, thread_id,
                          business_connection_id)
    except Exception as e:
        print(f"[ron_daemon] degraded notice failed: {e}", flush=True)


def _drain_queue():
    """Answer queued messages after recovery. True when the queue is empty.

    Only entries answered successfully are removed (matched by qid), so a
    mid-drain failure or a message queued concurrently is never lost or
    answered twice. Stale entries (>30 min old) are dropped with a log line.
    """
    try:
        with open(DEGRADED_QUEUE) as f:
            entries = [json.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return True
    except Exception as e:
        print(f"[ron_daemon] queue read failed: {e}", flush=True)
        return False
    if not entries:
        return True
    now = time.time()
    answered = set()
    # Purge stale entries up front so they can never block live ones behind
    # them in the queue.
    live = []
    for e in entries:
        if now - e.get("ts", 0) > QUEUE_MAX_AGE:
            print(f"[ron_daemon] dropping stale queued msg "
                  f"(chat={e.get('chat_id')})", flush=True)
            answered.add(e.get("qid"))
        else:
            live.append(e)
    for e in live:
        try:
            send_typing(e["chat_id"], e.get("business_connection_id"))
            _dq_private = e.get("private", True)
            _dq_extra = group_context()
            if not _dq_private and _MEM_MODULES:
                _dq_extra += ron_privacy.GROUP_PRIVACY_RULE
            reply = ron_ai.chat(e["chat_id"], e["text"],
                                e.get("user_name", "there"),
                                system_extra=_dq_extra,
                                user_id=e.get("user_id", 0),
                                is_private=_dq_private)
            send_rich_with_kb(e["chat_id"], reply, e.get("reply_to"),
                              e.get("thread_id"),
                              e.get("business_connection_id"))
            handle_group_sends(e["chat_id"], e.get("user_id"),
                               e.get("user_name", "there"), reply)
            handle_send_topic(e["chat_id"], e.get("user_id"),
                              e.get("user_name", "there"), reply)
            handle_topic_actions(e["chat_id"], e.get("user_name", "there"),
                                 reply,
                                 origin_group_id=(None if e.get("private", True)
                                                   else e["chat_id"]),
                                 origin_thread_id=(None if e.get("private", True)
                                                    else e.get("thread_id")))
            answered.add(e.get("qid"))
            _record_brain_success()
        except Exception as ex:
            print(f"[ron_daemon] queue replay failed, staying degraded: {ex}",
                  flush=True)
            break  # provider still sick — keep the rest queued
    with _QUEUE_LOCK:
        try:
            with open(DEGRADED_QUEUE) as f:
                current = [json.loads(l) for l in f if l.strip()]
        except FileNotFoundError:
            return not answered or len(answered) == len(entries)
        except Exception:
            return False
        rest = [e for e in current if e.get("qid") not in answered]
        try:
            tmp = DEGRADED_QUEUE + ".tmp"
            with open(tmp, "w") as f:
                for e in rest:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            os.replace(tmp, DEGRADED_QUEUE)
        except Exception as e:
            print(f"[ron_daemon] queue rewrite failed: {e}", flush=True)
            return False
    return not rest


def _recovery_loop():
    """While degraded, probe providers every minute; drain the queue on
    recovery."""
    while not _SHUTDOWN.is_set():
        time.sleep(RECOVERY_POLL_SECS)
        if _SHUTDOWN.is_set() or not _brain_down():
            continue
        try:
            results = ron_ai.probe_providers()
        except Exception as e:
            print(f"[ron_daemon] recovery probe failed: {e}", flush=True)
            continue
        _record_probe_results(results)
        healthy = [n for n, (ok, _) in results.items() if ok]
        if not healthy:
            continue
        print(f"[ron_daemon] providers back ({', '.join(healthy)}), "
              f"draining queue", flush=True)
        if _drain_queue():
            with _BRAIN_LOCK:
                _degraded["active"] = False
            print("[ron_daemon] DEGRADED MODE OFF: queue drained", flush=True)


def _health_ping():
    """Startup provider health check — DMs the owner if a key looks dead."""
    time.sleep(15)  # let the poll loop settle first
    try:
        results = ron_ai.probe_providers()
    except Exception as e:
        print(f"[ron_daemon] startup health probe failed: {e}", flush=True)
        return
    _record_probe_results(results)
    for name, (ok, detail) in results.items():
        print(f"[ron_daemon] health: {name} {'OK' if ok else 'DOWN'} - "
              f"{detail}", flush=True)
        if not ok and detail.startswith("HTTP 40"):
            try:
                send(OWNER_ID,
                     f"\u26A0\uFE0F Heads up: the {name} API key looks dead "
                     f"({detail}). The bot's running on the other providers "
                     f"for now.")
            except Exception as e:
                print(f"[ron_daemon] owner alert failed: {e}", flush=True)


def _track(t):
    with _INFLIGHT_LOCK:
        _INFLIGHT.add(t)


# ---------------------------------------------------------------------------
# Phase 4 — per-chat isolated memory (2026-09-20).
# Every row is scoped by chat_id; reads always filter by chat_id. A DM's
# memory never leaks into a group or another DM. Cross-chat disclosure
# happens ONLY via private-DM approval (never asked in the group — even the
# ask leaks metadata), and consent is recorded per (group, user, fact):
# ask once, never re-ask.
# ---------------------------------------------------------------------------
try:
    import ron_memory
    import ron_privacy
    _MEM_MODULES = True
except Exception as _mem_import_err:
    _MEM_MODULES = False
    print(f"[ron_daemon] memory modules unavailable: {_mem_import_err}",
          flush=True)
mem = None

# Phase 6: thread-local flag set by _memory_context when the prompt for
# THIS reply injected a consented cross-chat fact ("(approved to share
# here)"). Replies built on another chat's private facts must never be
# cached — the disclosure path bypasses the cache entirely. ai_reply runs
# each reply on its own worker thread, and _memory_context runs on that
# same thread via MEMORY_RECALL_HOOK, so a thread-local is race-free.
_CONSENT_DISCLOSED = threading.local()


def _consent_disclosed():
    return bool(getattr(_CONSENT_DISCLOSED, "flag", False))


def _mem_cfg():
    try:
        return ron_ai.load_config().get("memory", {}) or {}
    except Exception:
        return {}


def init_memory():
    """Start the memory store. Fail soft: the bot runs memory-less rather
    than dying. The store itself fails closed (no key -> it raises)."""
    global mem
    if not _MEM_MODULES:
        return
    if not _mem_cfg().get("enabled", True):
        print("[ron_daemon] memory: disabled in ron_ai.json", flush=True)
        return
    try:
        mem = ron_memory.MemoryStore()
    except Exception as e:
        # Never log key material — type name only.
        print(f"[ron_daemon] memory: disabled ({type(e).__name__})",
              flush=True)
        mem = None
        return
    ron_ai.MEMORY_HOOK = _memory_dual_write
    ron_ai.MEMORY_RECALL_HOOK = _memory_context
    # Phase 6: rolling history summaries live in the encrypted per-chat
    # memory DB (chat_meta table), never in a separate plaintext store.
    # The lambdas resolve the `mem` global at call time, so they stay
    # correct even if the store is recreated.
    ron_ai.MEMORY_SUMMARY_GET = (
        lambda chat_id: mem.get_meta(str(chat_id), "summary")
        if mem is not None else None)
    ron_ai.MEMORY_SUMMARY_SET = (
        lambda chat_id, text: mem.set_meta(str(chat_id), "summary", text)
        if mem is not None else None)
    threading.Thread(target=_approval_sweeper, daemon=True).start()
    print("[ron_daemon] memory: enabled", flush=True)


def _memory_dual_write(chat_id, user_id, user_name, user_text, reply):
    """Every completed turn lands in SQLite alongside the JSON history."""
    try:
        if mem is None:
            return
        c = str(chat_id)
        mem.record_message(c, user_id or 0, "user",
                           f"[{user_name}]: {user_text}")
        mem.record_message(c, 0, "assistant", reply)
    except Exception as e:
        print(f"[ron_daemon] memory write failed: {type(e).__name__}",
              flush=True)


def _memory_context(chat_id, user_text, is_private):
    """Bounded recall injected into the prompt. Always chat-scoped."""
    try:
        if mem is None:
            return ""
        cfg = _mem_cfg()
        lim = int(cfg.get("recall_limit", 3))
        chars = int(cfg.get("recall_chars", 700))
        bits = []
        # Phase 5: one ranked pass — facts weighted above messages, then
        # relevance-ranked history. Same bounded output format as before.
        for item in mem.recall_all(str(chat_id), user_text, limit=lim):
            if item["kind"] == "fact":
                bits.append("- " + item["text"])
            else:
                bits.append("- (earlier) " + item["text"][:160])
        if not is_private:
            # Facts P approved for this group — via consent rows only,
            # never by merging another chat's memory. Relevance-gated:
            # only injected when the current message is actually about
            # the fact's topic, same rule as the disclosure trigger.
            for _uid, ftext in mem.consented_facts(str(chat_id)):
                if ron_privacy.topic_matches(user_text, ftext):
                    bits.append("- (approved to share here) " + ftext[:200])
                    # Phase 6: this reply is built on another chat's
                    # private fact — mark it so the answer cache refuses
                    # to store it (disclosure path bypasses cache).
                    _CONSENT_DISCLOSED.flag = True
        if not bits:
            return ""
        return ("\nRelevant memory from this chat:\n"
                + "\n".join(bits))[:chars]
    except Exception:
        return ""


def _dm(user_id, text):
    """Best-effort DM. False when the bot can't open the chat (never
    started / blocked) — callers must stay silent, never degrade."""
    try:
        r = send(user_id, text)
        return bool(r.get("ok"))
    except Exception:
        return False


def _dm_approval(user_id, text, pending_id):
    """DM a Yes/No approval request. Returns True if Telegram accepted it.

    Preferred path: sendRichMessage with a blocks-form message — the ask
    as paragraph block(s) plus one buttons block per row (Yes =
    success/green, No = danger/red), rendering INSIDE the bubble. Any
    failure falls back to the classic plain message + inline keyboard
    (buttons under the bubble); the privacy guarantee holds either way
    (silent on failure, never degrade).
    """
    try:
        if ron_richblocks is None:
            raise RuntimeError("no blocks builder")
        blocks = ron_richblocks.text_blocks(text)
        blocks.extend(rich_buttons_blocks(
            ron_privacy.approval_button_rows(pending_id)))
        r = send_blocks(user_id, blocks)
        if r.get("ok"):
            return True
        print(f"[ron_daemon] rich approval send not ok: {str(r)[:120]}",
              flush=True)
    except Exception as e:
        print(f"[ron_daemon] rich approval send error: {e}", flush=True)
    try:
        r = send_markup(user_id, text,
                        ron_privacy.approval_keyboard(pending_id))
        return bool(r.get("ok"))
    except Exception:
        return False


def _maybe_trigger_disclosures(chat_id, text, message_id, group_title,
                               allow_disclose=False):
    """Silent disclosure flow. NOTHING is posted in the group here —
    approvals are asked via private DM. allow_disclose=True (the bot is
    already engaged in this reply cycle) additionally lets already-consented
    facts post as a reply.
    """
    if mem is None or not text:
        return
    cands = ron_privacy.find_disclosable_facts(mem, chat_id, text)
    if not cands:
        return
    timeout = int(_mem_cfg().get("approval_timeout_secs", 900))
    for c in cands:
        uid, fact = c["user_id"], c["fact"]
        if c.get("consented"):
            # Already approved for this group: disclose without re-asking,
            # but only when genuinely relevant and the bot is engaged.
            if allow_disclose and message_id:
                send_rich_with_kb(chat_id, fact["text"],
                                  reply_to=message_id)
                print(f"[ron_daemon] disclosure: consented fact shared "
                      f"(group={chat_id})", flush=True)
            continue
        if mem.active_pendings_for(uid, group_chat_id=chat_id,
                                   kind="disclosure", fact_id=fact["id"]):
            continue  # already asked, waiting on P
        pid = mem.create_pending(
            "disclosure", chat_id, uid, group_message_id=message_id,
            target_name=mem.member_name(chat_id, uid),
            fact_id=fact["id"], dm_chat_id=fact["chat_id"],
            timeout_secs=timeout)
        ok = _dm_approval(
            uid, ron_privacy.disclosure_dm_text(group_title, fact["text"]),
            pid)
        if not ok:
            # Can't DM P (never started the bot / blocked): stay silent,
            # log ids only — never the fact content.
            mem.set_pending_status(pid, "unreachable")
            print(f"[ron_daemon] disclosure: cannot DM user_id={uid}, "
                  f"staying silent (group={chat_id})", flush=True)
        else:
            print(f"[ron_daemon] disclosure: approval asked via DM "
                  f"(user_id={uid}, group={chat_id})", flush=True)


def _start_overshare_approval(chat_id, user_id, user_name, text, message_id,
                              group_title):
    """Adwaith asked something personal in a group: ask him privately via
    DM instead of answering publicly. Returns True (message consumed)."""
    if mem is None:
        return False
    timeout = int(_mem_cfg().get("approval_timeout_secs", 900))
    pid = mem.create_pending(
        "overshare", chat_id, user_id, group_message_id=message_id,
        target_name=user_name, payload=text, timeout_secs=timeout)
    ok = _dm_approval(
        user_id, ron_privacy.overshare_dm_text(group_title, text), pid)
    if not ok:
        mem.set_pending_status(pid, "unreachable")
        print(f"[ron_daemon] overshare: cannot DM user_id={user_id}, "
              f"staying silent (group={chat_id})", flush=True)
    else:
        print(f"[ron_daemon] overshare: approval asked via DM "
              f"(group={chat_id})", flush=True)
    return True


def _link_overshare_consent(group_chat_id, user_id, question):
    """Approving a personal answer also records consent for any DM fact it
    touched — ask once per fact per group, composed."""
    try:
        for f in mem.dm_facts(user_id):
            if ron_privacy.topic_matches(question, f["text"]):
                mem.record_consent(group_chat_id, user_id, f["id"])
    except Exception:
        pass


def _apply_approval(pending, approved):
    """Resolve a pending approval (from DM text or a Yes/No button)."""
    pid = pending["id"]
    uid = pending["target_user_id"]
    gid = pending["group_chat_id"]
    if approved:
        if pending["kind"] == "disclosure":
            fact = (mem.get_fact(pending["dm_chat_id"], pending["fact_id"])
                    if mem else None)
            if not fact or fact.get("superseded_by") is not None:
                mem.set_pending_status(pid, "expired")
                _dm(uid, "That note's gone from my memory — nothing to share.")
                return
            mem.record_consent(gid, uid, pending["fact_id"])
            mem.set_pending_status(pid, "disclosed")
            # Exactly one disclosure, as a reply to the triggering message.
            send_rich_with_kb(gid, fact["text"],
                              reply_to=pending["group_message_id"])
            _dm(uid, "Shared ✓ — I won't ask about that one in this group again.")
            print(f"[ron_daemon] disclosure: approved and shared "
                  f"(group={gid})", flush=True)
        else:  # overshare
            question = pending.get("payload") or ""
            mem.set_pending_status(pid, "approved")
            _link_overshare_consent(gid, uid, question)
            # Answer in the group, once, as a reply to his message.
            # Phase 6: cacheable=False — this answer may draw on personal
            # DM facts, so it must never be served from the answer cache.
            ai_reply(gid, uid, pending.get("target_name") or "there",
                     question, private=False,
                     reply_to=pending["group_message_id"], cacheable=False)
            _dm(uid, "Answering in the group ✓")
            print(f"[ron_daemon] overshare: approved, answering "
                  f"(group={gid})", flush=True)
    else:
        mem.set_pending_status(pid, "denied")
        _dm(uid, "Got it — kept private ✓")
        print(f"[ron_daemon] approval denied (kind={pending['kind']}, "
              f"group={gid})", flush=True)


def _resolve_dm_approval(user_id, text):
    """P answers a pending ask with text in DM. True if consumed."""
    if mem is None:
        return False
    t = (text or "").strip()
    if len(t) > 60:  # long DM text is conversation, not a Yes/No
        return False
    pendings = mem.active_pendings_for(user_id)
    if not pendings:
        return False
    verdict = ron_privacy.classify_response(t)
    if verdict is None:
        return False
    _apply_approval(pendings[0], approved=(verdict == "approve"))
    return True


def _record_turn(chat_id, user_id, user_name, user_text, reply):
    """Dual-write a non-AI turn (memory/command ack) to JSON + SQLite.

    Normal AI turns already dual-write via ron_ai._save_turn; this covers
    the handled paths that bypass the AI (remember/forget acks etc.) so
    the two stores stay in parity. Approval/disclosure DMs are system
    messages and are deliberately NOT recorded.
    """
    try:
        ron_ai._save_turn(chat_id, user_text, user_name, reply, user_id)
    except Exception:
        pass


def _handle_memory_nl(chat_id, user_id, user_name, text):
    """'remember this ...' / 'forget ...' / 'forget everything'.
    True if handled."""
    if mem is None:
        return False
    fact = ron_privacy.parse_remember(text)
    if fact is not None:
        mem.remember(chat_id, fact)
        ack = "Noted ✓ — I'll remember that for this chat."
        send(chat_id, ack)
        _record_turn(chat_id, user_id, user_name, text, ack)
        return True
    parsed = ron_privacy.parse_forget(text)
    if parsed is None:
        return False
    kind, q = parsed
    if kind == "all":
        mem.wipe_chat(chat_id)
        _clear_json_history(chat_id)
        ack = "Wiped this chat's memory ✓ — starting fresh."
    elif kind == "query":
        n = mem.forget(chat_id, q)
        ack = "Forgotten ✓" if n else "Nothing matching to forget."
    else:
        fid = mem.forget_latest_fact(chat_id)
        ack = ("Forgotten ✓" if fid
               else "There's nothing stored to forget.")
    send(chat_id, ack)
    _record_turn(chat_id, user_id, user_name, text, ack)
    return True


def _clear_json_history(chat_id):
    try:
        hist = ron_ai.load_history()
        hist.pop(str(chat_id), None)
        ron_ai.save_history(hist)
    except Exception:
        pass
    # Phase 6: the rolling summary is part of this chat's memory — a full
    # wipe clears it too. (wipe_chat already deletes chat_meta rows; this
    # covers any path that clears the JSON history on its own.)
    try:
        if mem is not None:
            mem.set_meta(str(chat_id), "summary", "")
    except Exception:
        pass


def _resolve_callback_approval(query, action, pid):
    """Yes/No button tapped in a DM. Only the target user can resolve."""
    if mem is None:
        return
    frm = query.get("from") or {}
    p = mem.get_pending(pid)
    if not p or p["status"] != "pending" or p["expires_ts"] < time.time():
        return  # stale or already resolved
    if frm.get("id") != p["target_user_id"]:
        print(f"[ron_daemon] approval button from wrong user (pid={pid})",
              flush=True)
        return
    _apply_approval(p, approved=(action == "go"))


def _approval_sweeper():
    """Expire stale approval asks; prune old raw messages daily."""
    last_prune = 0.0
    while not _SHUTDOWN.is_set():
        time.sleep(60)
        if _SHUTDOWN.is_set():
            break
        try:
            if mem is not None:
                mem.expire_due()
                now = time.time()
                if now - last_prune > 86400:
                    days = int(_mem_cfg().get("retention_days", 30))
                    n = mem.prune_expired(days)
                    if n:
                        print(f"[ron_daemon] memory: pruned {n} expired "
                              f"messages", flush=True)
                    last_prune = now
        except Exception as e:
            print(f"[ron_daemon] approval sweeper: {type(e).__name__}",
                  flush=True)


def _untrack(t):
    with _INFLIGHT_LOCK:
        _INFLIGHT.discard(t)


def _handle_signal(signum, frame):
    print(f"[ron_daemon] signal {signum}, draining in-flight replies...",
          flush=True)
    _SHUTDOWN.set()


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def api(method, params=None, timeout=65):
    """Telegram Bot API call over the keep-alive connection pool.

    Same contract as before (parsed JSON dict, or
    {"ok": False, "error": ...}), but reuses warm TLS connections instead
    of paying ~0.7s of proxy CONNECT + handshake per call."""
    body = urllib.parse.urlencode(params or {}).encode() if params else None
    try:
        status, data = http_pool.pool_for("api.telegram.org").post(
            f"/bot{_SURROGATE}/{method}", body,
            "application/x-www-form-urlencoded", timeout)
    except Exception as e:
        return {"ok": False, "error": str(e)[:150]}
    if status != 200:
        return {"ok": False, "error":
                f"HTTP {status}: {data[:120].decode('utf-8', 'replace')}"[:150]}
    try:
        return json.loads(data.decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"bad JSON: {e}"[:150]}


def send(chat_id, text, reply_to=None, thread_id=None,
         business_connection_id=None):
    params = {"chat_id": chat_id, "text": text}
    if reply_to:
        # api() urlencodes, so nested objects must be pre-serialized to JSON
        params["reply_parameters"] = json.dumps({"message_id": reply_to})
    if thread_id:
        params["message_thread_id"] = thread_id
    if business_connection_id:
        # Secretary Mode: send on the business account, not as the bot.
        params["business_connection_id"] = business_connection_id
    return api("sendMessage", params)


def send_markup(chat_id, text, reply_markup, reply_to=None,
                business_connection_id=None):
    params = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": json.dumps(reply_markup),
    }
    if reply_to:
        params["reply_parameters"] = json.dumps({"message_id": reply_to})
    if business_connection_id:
        params["business_connection_id"] = business_connection_id
    return api("sendMessage", params)


def send_typing(chat_id, business_connection_id=None):
    """Show 'typing...' at the top of the chat. Telegram expires it after
    ~5s, so long generations must resend it periodically."""
    try:
        params = {"chat_id": chat_id, "action": "typing"}
        if business_connection_id:
            params["business_connection_id"] = business_connection_id
        api("sendChatAction", params)
    except Exception:
        pass


def typing_kickoff(chat_id, business_connection_id=None):
    """Show 'typing…' without blocking the AI call.

    The indicator is pure UX — the AI request is the long pole, so it
    starts immediately while Telegram catches up in the background.
    Returns the thread; join it (with a timeout) after the AI finishes
    so reply ordering stays deterministic."""
    t = threading.Thread(target=send_typing,
                         args=(chat_id, business_connection_id), daemon=True)
    t.start()
    return t


BTN_RE = re.compile(r"^\[buttons:\s*(.+?)\s*\]\s*$", re.M | re.I)
LINK_RE = re.compile(r"^\[links:\s*(.+?)\s*\]\s*$", re.M | re.I)
# trailers the model tacks onto the end of a sentence instead of their own line
BTN_TAIL_RE = re.compile(r"\[buttons:\s*([^\]]+?)\s*\]\s*$", re.I)
LINK_TAIL_RE = re.compile(r"\[links:\s*([^\]]+?)\s*\]\s*$", re.I)


# ---------------------------------------------------------------------------
# DM-to-group management: the bot learns every group it sees and can post
# there on a user's behalf when they ask in DM ("ping me in the playground
# group"). The model emits [send_group: Group Name | message]; the daemon
# resolves the group and posts the message mentioning the requester.
# Only the requester themself is ever mentioned — no pinging other people.
# ---------------------------------------------------------------------------
KNOWN_GROUPS_FILE = os.path.join(HOME, "workspace", "telegram-bot",
                                 "known_groups.json")

GROUP_RE = re.compile(r"^\[send_group:\s*(.+?)\s*\]\s*$", re.M | re.I)
GROUP_TAIL_RE = re.compile(r"\[send_group:\s*([^\]]+?)\s*\]\s*$", re.I)


def load_known_groups():
    try:
        with open(KNOWN_GROUPS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def note_group(chat_id, title):
    """Remember a group/supergroup the bot can post in."""
    if not chat_id or not title:
        return
    try:
        with _FILE_LOCK:
            groups = load_known_groups()
            groups[str(chat_id)] = {"title": title, "seen": time.time()}
            _atomic_write_json(KNOWN_GROUPS_FILE, groups)
    except Exception as e:
        print(f"[ron_daemon] note_group error: {e}", flush=True)


# Forum status per group, cached so the bot notices when topics get enabled
# or disabled in a group without calling getChat on every message.
_FORUM_STATUS = {}  # group_id -> (is_forum, checked_at)
_FORUM_TTL = 600


def forum_status(group_id):
    """True/False/None: is this group currently a forum? Cached via getChat."""
    key = str(group_id)
    now = time.time()
    hit = _FORUM_STATUS.get(key)
    if hit and now - hit[1] < _FORUM_TTL:
        return hit[0]
    val = None
    r = api("getChat", {"chat_id": group_id}, timeout=20)
    if r.get("ok"):
        val = bool((r.get("result") or {}).get("is_forum"))
        _FORUM_STATUS[key] = (val, now)
    return val


def purge_topics(group_id):
    """Forget every topic remembered for a group (topics were disabled)."""
    with _FILE_LOCK:
        topics = load_known_topics()
        if topics.pop(str(group_id), None) is not None:
            save_known_topics(topics)
            _FORUM_STATUS[str(group_id)] = (False, time.time())
            print(f"[ron_daemon] topics disabled in {group_id}, purged",
                  flush=True)


def group_context():
    """One-liner for the AI listing groups (and their topics) it may post to.

    Topic info only appears for groups that are currently forums — when a
    group drops its topics, the bot forgets them and stops acting like
    topics exist there.
    """
    groups = load_known_groups()
    if not groups:
        return ""
    topics = load_known_topics()
    bits = []
    forum_bits = []
    for cid in sorted(groups, key=lambda k: groups[k].get("title", "")):
        title = groups[cid].get("title") or "?"
        fs = _FORUM_STATUS.get(str(cid), (None, 0))[0]
        if fs is False:
            tnames = []
        else:
            tnames = [t["name"] for _, t in sorted(
                topics.get(cid, {}).items(), key=lambda x: int(x[0]))
                if t.get("name")]
        bits.append(title + (" (topics: " + ", ".join(tnames) + ")" if tnames
                             else ""))
        if fs is True or (fs is None and tnames):
            forum_bits.append(title)
    ctx = ("\nGroups you can post to with [send_group: ...]: "
           + "; ".join(bits) + ".")
    if forum_bits:
        ctx += (" Topic-capable (forum) groups: " + ", ".join(forum_bits)
                + ". You can manage topics there with trailers, one per line: "
                "[topic: Group Name | create | Topic Name], "
                "[topic: Group Name | close | Topic Name], "
                "[topic: Group Name | reopen | Topic Name], "
                "[topic: Group Name | rename | New Name] (current topic) or "
                "[topic: Group Name | rename | Old Topic | New Name], "
                "[topic: Group Name | delete | Topic Name], "
                "and post into a specific topic with "
                "[send_topic: Group Name | Topic Name | message] "
                "(mentions the requester). "
                "Only use group and topic names from your context list, "
                "exactly as listed. "
                "'This topic' means the topic you're currently replying in. "
                "Deleting a topic wipes all its messages — always ask for "
                "explicit confirmation before emitting a delete trailer.")
    return ctx


# ---------------------------------------------------------------------------
# Forum topics: track topic id <-> name per group, reply inside the same
# topic, and manage topics (create/rename/close/reopen/delete) via trailers.
# ---------------------------------------------------------------------------
KNOWN_TOPICS_FILE = os.path.join(HOME, "workspace", "telegram-bot",
                                 "known_topics.json")

TOPIC_RE = re.compile(r"^\[topic:\s*(.+?)\s*\]\s*$", re.M | re.I)
TOPIC_TAIL_RE = re.compile(r"\[topic:\s*([^\]]+?)\s*\]\s*$", re.I)
SEND_TOPIC_RE = re.compile(r"^\[send_topic:\s*(.+?)\s*\]\s*$", re.M | re.I)
SEND_TOPIC_TAIL_RE = re.compile(r"\[send_topic:\s*([^\]]+?)\s*\]\s*$", re.I)


def load_known_topics():
    try:
        with open(KNOWN_TOPICS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_known_topics(t):
    # Callers hold _FILE_LOCK (read-modify-write); the write itself is atomic.
    try:
        _atomic_write_json(KNOWN_TOPICS_FILE, t)
    except Exception as e:
        print(f"[ron_daemon] save topics error: {e}", flush=True)


def note_topic(group_id, thread_id, name=None, closed=None):
    """Remember a forum topic the bot has seen."""
    if not group_id or not thread_id:
        return
    with _FILE_LOCK:
        topics = load_known_topics()
        g = topics.setdefault(str(group_id), {})
        tid = str(thread_id)
        t = g.setdefault(tid, {"name": "General" if tid == "1" else f"topic {tid}"})
        if name:
            t["name"] = name
        if closed is not None:
            t["closed"] = bool(closed)
        save_known_topics(topics)


def topic_name(group_id, thread_id):
    return (load_known_topics().get(str(group_id), {})
            .get(str(thread_id), {}).get("name"))


# ---------------------------------------------------------------------------
# Business / Secretary Mode: the bot can reply inside the owner's business
# account chats (Chat Automation). BusinessConnection updates are persisted
# here (atomic write + _FILE_LOCK, same pattern as known_groups.json);
# business_message updates are answered like DMs, but every outgoing send
# carries business_connection_id so the reply goes out on the business
# account. Only connection metadata is stored — never message content.
# ---------------------------------------------------------------------------
BUSINESS_CONNECTIONS_FILE = os.path.join(HOME, "workspace", "telegram-bot",
                                         "business_connections.json")


def load_business_connections():
    try:
        with open(BUSINESS_CONNECTIONS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_business_connections(conns):
    # Callers hold _FILE_LOCK (read-modify-write); the write itself is atomic.
    try:
        _atomic_write_json(BUSINESS_CONNECTIONS_FILE, conns)
    except Exception as e:
        print(f"[ron_daemon] save business connections error: {e}",
              flush=True)


def handle_business_connection(bc):
    """Track (or drop) a business connection from a business_connection update."""
    bc = bc or {}
    conn_id = bc.get("id")
    if not conn_id:
        return
    with _FILE_LOCK:
        conns = load_business_connections()
        if bc.get("is_enabled"):
            # can_reply lives on BusinessBotRights in the current Bot API;
            # older payloads carried it directly on the connection object.
            rights = bc.get("rights") or {}
            can_reply = bc.get("can_reply")
            if can_reply is None:
                can_reply = rights.get("can_reply", True)
            user = bc.get("user") or {}
            conns[conn_id] = {
                "user_chat_id": bc.get("user_chat_id"),
                "can_reply": bool(can_reply),
                "user_id": user.get("id"),
                "first_name": user.get("first_name", ""),
                "date": bc.get("date", 0),
            }
            print(f"[ron_daemon] business connection {conn_id}: enabled, "
                  f"can_reply={bool(can_reply)}", flush=True)
        else:
            if conns.pop(conn_id, None) is not None:
                print(f"[ron_daemon] business connection {conn_id}: "
                      "disabled, dropped", flush=True)
        save_business_connections(conns)


def handle_business_message(msg):
    """Reply inside the owner's business account chats.

    Answers the other party's messages like a DM (normal AI flow), but the
    reply goes out on the business account via business_connection_id.
    The owner's own messages and our own echoes are skipped so the bot
    never talks over the human or loops on itself."""
    msg = msg or {}
    conn_id = msg.get("business_connection_id")
    if not conn_id:
        return
    conn = load_business_connections().get(conn_id)
    if not conn:
        print(f"[ron_daemon] business message for unknown connection "
              f"{conn_id}, ignoring", flush=True)
        return
    if not conn.get("can_reply"):
        print(f"[ron_daemon] business connection {conn_id}: can_reply=false, "
              "ignoring", flush=True)
        return
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return
    frm = msg.get("from") or {}
    sender = frm.get("id", 0)
    # Skip the business owner's own messages and any bot's echoes.
    if sender and (sender == conn.get("user_id") or frm.get("is_bot")):
        return
    # Maintenance mode covers business chats too — customers wait quietly.
    if maintenance_on():
        return
    ai_reply(chat_id, sender, frm.get("first_name", "there"), text,
             private=True, reply_to=msg.get("message_id"),
             business_connection_id=conn_id)


def resolve_topic(group_id, name):
    """Fuzzy topic name -> thread id. Returns (thread_id, error)."""
    topics = load_known_topics().get(str(group_id), {})
    key = name.strip().lower()
    if key == "general":
        return 1, None
    matches = [tid for tid, t in topics.items()
               if key in (t.get("name") or "").lower()]
    if len(matches) == 1:
        return int(matches[0]), None
    known = ", ".join(t.get("name", "?") for t in topics.values())
    if not matches:
        return None, f"No topic '{name}' — topics here: {known or 'none yet'}."
    names = ", ".join(topics[m].get("name", "?") for m in matches)
    return None, f"'{name}' matches: {names} — which one?"


def forum_api(method, payload):
    """Call a forum Bot API method. Returns (ok, result_or_error)."""
    try:
        r = rich_mod.api_json(method, payload)
        if r.get("ok"):
            return True, r.get("result")
        desc = r.get("description") or r.get("error") or "unknown error"
        print(f"[ron_daemon] {method} failed: {str(r)[:150]}", flush=True)
        return False, str(desc)[:160]
    except Exception as e:
        print(f"[ron_daemon] {method} error: {e}", flush=True)
        return False, str(e)[:160]


def handle_topic_actions(origin_chat_id, user_name, reply,
                         origin_group_id=None, origin_thread_id=None):
    """Execute [topic: Group | action | args...] trailers the model emitted."""
    for m in TOPIC_RE.finditer(reply):
        parts = [x.strip() for x in m.group(1).split("|")]
        if len(parts) < 2:
            continue
        gname, action = parts[0], parts[1].lower()
        args = parts[2:]
        gid, err = resolve_group(gname)
        if err:
            send(origin_chat_id, err)
            continue
        if forum_status(gid) is False:
            send(origin_chat_id,
                 f"{gname} isn't a forum group anymore — topics are off there.")
            continue

        def need_thread(arg):
            # named topic, or the topic the request came from
            if arg:
                return resolve_topic(gid, arg)
            if origin_group_id and str(origin_group_id) == str(gid) \
                    and origin_thread_id:
                return origin_thread_id, None
            return None, ("Which topic? Name it, e.g. "
                          f"[topic: {gname} | {action} | Topic Name].")

        if action == "create":
            if not args:
                continue
            ok, res = forum_api("createForumTopic",
                                {"chat_id": gid, "name": args[0][:128]})
            if ok:
                tid = (res or {}).get("message_thread_id")
                if tid:
                    note_topic(gid, tid, name=args[0])
                print(f"[ron_daemon] topic created '{args[0]}' in {gid}",
                      flush=True)
            else:
                send(origin_chat_id, f"Couldn't create the topic: {res}")
        elif action in ("close", "reopen"):
            tid, terr = need_thread(args[0] if args else "")
            if terr:
                send(origin_chat_id, terr)
                continue
            method = ("closeForumTopic" if action == "close"
                      else "reopenForumTopic")
            ok, res = forum_api(method, {"chat_id": gid,
                                         "message_thread_id": tid})
            if ok:
                note_topic(gid, tid, closed=(action == "close"))
                print(f"[ron_daemon] topic {action}d {tid} in {gid}",
                      flush=True)
            else:
                send(origin_chat_id, f"Couldn't {action} the topic: {res}")
        elif action == "rename":
            if len(args) == 2:
                tid, terr = resolve_topic(gid, args[0])
                new_name = args[1]
            elif len(args) == 1:
                tid, terr = need_thread("")
                new_name = args[0]
            else:
                continue
            if terr:
                send(origin_chat_id, terr)
                continue
            ok, res = forum_api("editForumTopic",
                                {"chat_id": gid, "message_thread_id": tid,
                                 "name": new_name[:128]})
            if ok:
                note_topic(gid, tid, name=new_name)
                print(f"[ron_daemon] topic {tid} renamed to '{new_name}'",
                      flush=True)
            else:
                send(origin_chat_id, f"Couldn't rename the topic: {res}")
        elif action == "delete":
            tid, terr = need_thread(args[0] if args else "")
            if terr:
                send(origin_chat_id, terr)
                continue
            tname = topic_name(gid, tid) or f"topic {tid}"
            ok, res = forum_api("deleteForumTopic",
                                {"chat_id": gid, "message_thread_id": tid})
            if ok:
                with _FILE_LOCK:
                    topics = load_known_topics()
                    topics.get(str(gid), {}).pop(str(tid), None)
                    save_known_topics(topics)
                print(f"[ron_daemon] topic '{tname}' deleted in {gid}",
                      flush=True)
            else:
                send(origin_chat_id, f"Couldn't delete the topic: {res}")


def handle_send_topic(origin_chat_id, user_id, user_name, reply):
    """Execute [send_topic: Group | Topic | message] trailers."""
    for m in SEND_TOPIC_RE.finditer(reply):
        parts = [x.strip() for x in m.group(1).split("|", 2)]
        if len(parts) < 3 or not all(parts):
            continue
        gname, tname, message = parts
        gid, err = resolve_group(gname)
        if err:
            send(origin_chat_id, err)
            continue
        if forum_status(gid) is False:
            send(origin_chat_id,
                 f"{gname} isn't a forum group anymore — topics are off there.")
            continue
        tid, terr = resolve_topic(gid, tname)
        if terr:
            send(origin_chat_id, terr)
            continue
        name = (user_name or "you").strip() or "you"
        text = f"{name} \u2014 {message.strip()}"
        entities = [{"type": "text_mention", "offset": 0,
                     "length": _u16_len(name), "user": {"id": user_id}}]
        ok, res = forum_api("sendMessage", {
            "chat_id": gid, "message_thread_id": tid,
            "text": text[:4000], "entities": entities})
        if ok:
            print(f"[ron_daemon] topic ping -> {tname} in {gid}", flush=True)
        else:
            send(origin_chat_id, f"Couldn't post in {tname}: {res}")


def resolve_group(name_or_id):
    """Return chat_id for a group name (fuzzy) or numeric id. (chat_id, err)"""
    groups = load_known_groups()
    key = name_or_id.strip()
    if key.lstrip("-").isdigit() and key in groups:
        return int(key), None
    matches = [cid for cid, g in groups.items()
               if key.lower() in (g.get("title") or "").lower()]
    if len(matches) == 1:
        return int(matches[0]), None
    if not matches:
        known = ", ".join(sorted(g["title"] for g in groups.values()
                                 if g.get("title"))) or "none yet"
        return None, f"I don't see a group called '{key}'. I'm in: {known}."
    titles = ", ".join(groups[c]["title"] for c in matches)
    return None, f"'{key}' matches several groups: {titles} — which one?"


def send_group_ping(group_chat_id, user_id, first_name, message):
    """Post message in a group, mentioning the requester so it pings them."""
    name = (first_name or "you").strip() or "you"
    text = f"{name} \u2014 {message.strip()}"
    entities = [{"type": "text_mention", "offset": 0,
                 "length": _u16_len(name), "user": {"id": user_id}}]
    try:
        # NOTE: entities is nested — must go over JSON, not the urlencode api().
        r = rich_mod.api_json("sendMessage", {
            "chat_id": group_chat_id,
            "text": text[:4000],
            "entities": entities,
        })
        if r.get("ok"):
            return True
        print(f"[ron_daemon] group ping not ok: {str(r)[:150]}", flush=True)
    except Exception as e:
        print(f"[ron_daemon] group ping error: {e}", flush=True)
    return False


def handle_group_sends(origin_chat_id, user_id, user_name, reply):
    """Execute [send_group: ...] trailers the model emitted."""
    for m in GROUP_RE.finditer(reply):
        inner = m.group(1)
        if "|" not in inner:
            continue
        gname, message = (x.strip() for x in inner.split("|", 1))
        if not gname or not message:
            continue
        gid, err = resolve_group(gname)
        if err:
            send(origin_chat_id, err)
            continue
        title = load_known_groups().get(str(gid), {}).get("title", "the group")
        if send_group_ping(gid, user_id, user_name, message):
            print(f"[ron_daemon] group ping -> {title} for {user_id}",
                  flush=True)
        else:
            send(origin_chat_id, f"Couldn't post in {title} — try again?")


def strip_trailer_syntax(reply):
    """Remove every trailer form: [buttons:...]/[links:...]/[send_group:...]
    /[send_topic:...]/[topic:...], line or inline."""
    reply = BTN_RE.sub("", LINK_RE.sub("", reply))
    reply = GROUP_RE.sub("", reply)
    reply = SEND_TOPIC_RE.sub("", reply)
    reply = TOPIC_RE.sub("", reply)
    reply = BTN_TAIL_RE.sub("", LINK_TAIL_RE.sub("", reply))
    reply = GROUP_TAIL_RE.sub("", reply)
    reply = SEND_TOPIC_TAIL_RE.sub("", reply)
    return TOPIC_TAIL_RE.sub("", reply).strip()


def _clean_btn_label(s):
    """Button labels are plain text — strip Markdown the AI sometimes leaks
    in (**bold**, `code`, ||spoiler||, ==highlight==) and collapse spaces."""
    s = re.sub(r"(\*\*|__|`|~~|\|\||==)", "", s or "")
    return re.sub(r"\s+", " ", s).strip()


def parse_trailers(reply):
    """Pull [buttons: ...] / [links: ...] trailer lines into an inline keyboard."""
    btns = (BTN_RE.search(reply) or BTN_TAIL_RE.search(reply) or [None, None])[1]
    links = (LINK_RE.search(reply) or LINK_TAIL_RE.search(reply) or [None, None])[1]
    clean = strip_trailer_syntax(reply)
    kb = []
    link_row = []
    # Spoiler markers contain the "|" separator — erase them before the
    # split, or "||x||" shatters into phantom buttons.
    if btns:
        btns = btns.replace("||", "")
    if btns:
        # callback_data is capped at 64 bytes by Telegram — truncate by bytes,
        # not chars, so multibyte labels can't produce BUTTON_DATA_INVALID.
        row = []
        for b in (x.strip() for x in btns.split("|")):
            if not b:
                continue
            # The AI sometimes puts link syntax inside [buttons: ...] —
            # rescue it as a real URL button instead of showing raw junk.
            if "->" in b:
                label, url = (x.strip() for x in b.split("->", 1))
                if url.startswith("http"):
                    link_row.append({"text": _clean_btn_label(label)[:40],
                                     "url": url})
                    continue
            label = _clean_btn_label(b)
            if label:
                row.append({"text": label[:40],
                            "callback_data": _cb64("q:" + label)})
            if len(row) >= 8:
                break
        if row:
            kb.append(row)
    if links:
        for part in (x.strip() for x in links.split("|") if x.strip()):
            if "->" not in part:
                continue
            label, url = (x.strip() for x in part.split("->", 1))
            if url.startswith("http"):
                link_row.append({"text": _clean_btn_label(label)[:40],
                                 "url": url})
    if link_row:
        kb.append(link_row[:8])
    return clean, {"inline_keyboard": kb} if kb else None


# ---------------------------------------------------------------------------
# Rich message buttons (Bot API 10.3) — used "whenever necessary".
#
# In-bubble buttons REQUIRE the blocks form of rich_message
# ({"blocks": [...]}); the classic reply_markup keyboard always renders
# UNDER the bubble. So: messages that carry buttons go out as blocks
# (paragraph/blockquote/list/pre blocks + one buttons block per row).
# ---------------------------------------------------------------------------
_RICH_BTN_STYLES = ("primary", "success", "danger", "link")
# RichMessageButton takes the same actions as an inline keyboard button.
_RICH_BTN_ACTIONS = ("callback_data", "url", "web_app", "login_url",
                     "switch_inline_query",
                     "switch_inline_query_current_chat",
                     "switch_inline_query_chosen_chat", "copy_text")


def rich_buttons_block(row, align="left"):
    """Build ONE InputRichBlockButtons block from a single row (1-8 buttons).

    Each button holds {"text", exactly ONE action from _RICH_BTN_ACTIONS,
    optional "style"}. The block renders as a button row INSIDE the message
    bubble — this is the fix for buttons landing under the bubble.
    """
    if align not in ("left", "center", "right"):
        align = "left"
    if not row or len(row) > 8:
        raise ValueError("rich button row must hold 1-8 buttons")
    btns = []
    for b in row:
        actions = [k for k in _RICH_BTN_ACTIONS if b.get(k) is not None]
        if len(actions) != 1:
            raise ValueError("RichMessageButton needs exactly one action")
        rb = {"text": (b.get("text") or "")[:64]}
        if actions[0] == "callback_data":
            # 64-byte cap, UTF-8 safe (same rule as inline keyboards).
            rb["callback_data"] = _cb64(b["callback_data"])
        else:
            rb[actions[0]] = b[actions[0]]
        style = b.get("style")
        if style in _RICH_BTN_STYLES:
            rb["style"] = style
        elif actions[0] == "callback_data":
            rb["style"] = "primary"
        btns.append(rb)
    return {"type": "buttons", "buttons": btns, "align": align}


def rich_buttons_blocks(rows, align="left"):
    """One InputRichBlockButtons block per row (Telegram: 1-8 per row)."""
    if not rows:
        raise ValueError("no buttons to build a block from")
    return [rich_buttons_block(row, align) for row in rows]


def rich_text_block(text):
    """Single paragraph block. (The old {"type": "text", ...} shape was
    rejected by the API — "type text is unsupported". Verified 2026-09-20:
    the text block is {"type": "paragraph", "text": RichText}.)"""
    return {"type": "paragraph", "text": text or ""}


def strip_markdown(t):
    """Degrade rich Markdown to plain text for the no-format fallback."""
    t = re.sub(r"```\w*\n?", "", t)
    t = re.sub(r"[*_`~]", "", t)
    t = re.sub(r"^#{1,6}\s+", "", t, flags=re.M)
    t = re.sub(r"^>\s?", "", t, flags=re.M)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    return t.strip()


def send_rich(chat_id, text, reply_markup=None, reply_to=None, thread_id=None,
              business_connection_id=None):
    """Native rich message (Bot API 10.1); falls back to plain text."""
    rp = {"reply_parameters": {"message_id": reply_to}} if reply_to else {}
    try:
        kw = {"reply_markup": reply_markup} if reply_markup else {}
        if thread_id:
            kw["message_thread_id"] = thread_id
        if business_connection_id:
            # Secretary Mode: rich send on the business account. If the
            # business user can't send rich messages the API 400s and we
            # fall through to the plain-text fallback below (which also
            # carries the connection id).
            kw["business_connection_id"] = business_connection_id
        r = rich_mod.rich_send(chat_id, text, fmt="markdown", **kw, **rp)
        if r.get("ok"):
            return r
        print(f"[ron_daemon] rich send not ok: {str(r)[:120]}", flush=True)
    except Exception as e:
        print(f"[ron_daemon] rich send error: {e}", flush=True)
    plain = strip_markdown(text)
    if reply_markup:
        r2 = send_markup(chat_id, plain, reply_markup, reply_to,
                         business_connection_id)
        if r2.get("ok"):
            return r2
        # Bad keyboard (oversized button etc.) — drop it, keep the text.
        print(f"[ron_daemon] markup send not ok, retrying plain: "
              f"{str(r2)[:120]}", flush=True)
    return send(chat_id, plain, reply_to, thread_id, business_connection_id)


# ---------------------------------------------------------------------------
# Direct commands.
# /ping and /id are answered here (cheap, no LLM call — /id genuinely
# needs code since the model doesn't know the user's numeric Telegram id),
# plus /poll, /pollm, /quiz (structured sendPoll, no LLM needed).
# Everything else (/start, /help, ...) stays conversational per Adwaith's
# rule: the bot acts like an AI, not scripted responses.
# ---------------------------------------------------------------------------
def parse_command(text):
    """Return the command name for '/cmd' or '/cmd@ronrebootedbot', else None.
    Commands addressed to a different bot ('/cmd@otherbot') are ignored."""
    if not text.startswith("/"):
        return None
    rest = text[1:].split(None, 1)
    if not rest:
        return None
    token = rest[0]
    if "@" in token:
        token, _, target = token.partition("@")
        if target.lower() != BOT_USERNAME.lower():
            return None
    return token.lower() or None


def handle_command(chat_id, user_id, cmd, is_private, text="",
                   user_name="there", reply_to=None, thread_id=None):
    if cmd == "shutdown":
        # Owner-only maintenance mode: the bot goes quiet for everyone
        # except the owner. /start (owner) wakes it back up.
        if user_id != OWNER_ID:
            send(chat_id, "Only Adwaith can shut me down.")
            return True
        set_maintenance(True, user_id)
        send(chat_id, "Maintenance mode on \U0001F6CC — quiet for everyone "
                      "but you. Send /start to wake me back up.")
        return True
    if cmd == "start":
        if maintenance_on():
            if user_id != OWNER_ID:
                send(chat_id, "Ron is taking a break \U0001F6CC — back soon.")
                return True
            set_maintenance(False, user_id)
            send(chat_id, "Back online \u2014 everyone can talk to me again.")
            return True
        # Normally /start stays conversational (not a scripted greeting).
        return False
    if cmd == "ping":
        send(chat_id, "\U0001F3D3 pong \u2014 alive and polling.")
        return True
    if cmd == "health":
        # Phase 7: compact status card. Cheap local reads only — no AI
        # call, works in light mode and degraded mode alike.
        return _handle_health(chat_id)
    if cmd in ("poll", "pollm", "quiz"):
        return handle_poll_command(chat_id, cmd, text)
    if cmd == "id":
        text = f"Your Telegram user id is {user_id}"
        if is_private:
            send(chat_id, text)
        else:
            # Ephemeral: only the requester sees it, zero group spam.
            # NOTE: must go over JSON — the urlencode api() mangles nested dicts.
            rich_mod.api_json("sendMessage", {
                "chat_id": chat_id,
                "text": text,
                "ephemeral_message_parameters": {"receiver_user_id": user_id},
            })
        return True
    if cmd == "imagine":
        return _handle_imagine(chat_id, user_id, user_name, text,
                               is_private, reply_to=reply_to,
                               thread_id=thread_id)
    if cmd == "factcheck":
        return _handle_factcheck(chat_id, user_id, user_name, text,
                                 is_private, reply_to=reply_to,
                                 thread_id=thread_id)
    if cmd == "think":
        return _handle_think(chat_id, user_id, user_name, text,
                             is_private, reply_to=reply_to,
                             thread_id=thread_id)
    if cmd == "forget":
        # Phase 4: per-chat memory wipe. In a DM anyone can wipe their own
        # DM; in a group only the owner (group memory is shared).
        if mem is None:
            send(chat_id, "Memory isn't enabled right now.")
            return True
        if not is_private and user_id != OWNER_ID:
            send(chat_id, "Only Adwaith can wipe this group's memory.")
            return True
        mem.wipe_chat(chat_id)
        _clear_json_history(chat_id)
        ack = "Wiped this chat's memory ✓ — starting fresh."
        send(chat_id, ack)
        _record_turn(chat_id, user_id, user_name, text, ack)
        return True
    return False


def _handle_health(chat_id):
    """Phase 7 /health: assemble status info and send the card.

    All reads are local and cheap (no AI call). Every section is
    best-effort — a broken JSON file or missing module degrades to "n/a",
    never to an exception, because a status command must not fail.
    Provider names never leave this function: the card uses chain
    positions only (primary / fallback-N).
    """
    if ron_health is None:
        send(chat_id, "health status unavailable (module missing)")
        return True
    info = {"start_ts": _START_TS}
    try:
        info["degraded"] = _brain_down()
    except Exception:
        pass
    try:
        chain = (ron_ai.load_config().get("providers")
                 or ron_ai.DEFAULT_PROVIDERS)
        cooldowns = ron_ai.get_provider_cooldowns()
        with _PROBE_LOCK:
            probe_res = dict(_LAST_PROBE)
        now_m = time.monotonic()
        providers = []
        for i, pr in enumerate(chain):
            name = str(pr.get("name") or "").lower()
            label = "primary" if i == 0 else f"fallback-{i}"
            until = cooldowns.get(name, 0)
            if until > now_m:
                providers.append((label, "cooling", until - now_m))
            elif name in probe_res:
                ok, detail = probe_res[name]
                if ok:
                    providers.append((label, "ok", None))
                elif "no key" in str(detail or ""):
                    providers.append((label, "no_key", None))
                else:
                    providers.append((label, "down", None))
            else:
                providers.append((label, "unknown", None))
        info["providers"] = providers
    except Exception:
        pass
    try:
        info["memory_on"] = mem is not None
        if mem is not None:
            info["summaries"] = mem.count_summaries()
    except Exception:
        pass
    try:
        if ron_cache is not None:
            st = ron_cache.get_stats()
            info["cache_hits"] = st.get("hits")
            info["cache_misses"] = st.get("misses")
            info["cache_tokens"] = st.get("tokens_saved_est")
    except Exception:
        pass
    try:
        if ron_vision is not None:
            info["images_24h"] = ron_vision.stats().get("images_24h")
    except Exception:
        pass
    try:
        if ron_imagen is not None:
            info["images_gen_24h"] = ron_imagen.stats().get("images_gen_24h")
    except Exception:
        pass
    try:
        with open(os.path.join(HOME, "workspace", "telegram-bot",
                               "ron_backup.json"),
                  "r", encoding="utf-8") as f:
            bst = json.load(f)
            info["backup_ts"] = bst.get("last_good_ts")
            info["backup_error"] = bst.get("last_error")
    except Exception:
        pass
    try:
        with open(os.path.join(HOME, "workspace", "telegram-bot",
                               "ron_watchdog.json"),
                  "r", encoding="utf-8") as f:
            info["restarts"] = json.load(f).get("total_restarts")
    except Exception:
        pass
    try:
        card = ron_health.build_health_card(info)
    except Exception as e:
        print(f"[ron_daemon] health card failed: {e}", flush=True)
        card = "Ron health: status temporarily unavailable."
    send(chat_id, card)
    return True


BOT_USERNAME = "ronrebootedbot"


POLL_USAGE = (
    "Poll format:\n"
    "/poll Question? | Option A | Option B\n"
    "/pollm Question? | A | B | C  (multiple answers allowed)\n"
    "/quiz Question? | *Correct | Wrong one | Wrong two"
)


def send_poll(chat_id, question, options, quiz=False, correct_idx=0,
              multi=False):
    """Native Telegram poll via sendPoll. Returns True on ok."""
    try:
        payload = {
            "chat_id": chat_id,
            "question": question.strip()[:300],
            "options": [o.strip()[:100] for o in options],
            "is_anonymous": True,
        }
        if quiz:
            payload["type"] = "quiz"
            payload["correct_option_id"] = correct_idx
        elif multi:
            payload["allows_multiple_answers"] = True
        # NOTE: must go over JSON — the urlencode api() mangles the options list.
        r = rich_mod.api_json("sendPoll", payload)
        if r.get("ok"):
            return True
        print(f"[ron_daemon] sendPoll not ok: {str(r)[:150]}", flush=True)
    except Exception as e:
        print(f"[ron_daemon] sendPoll error: {e}", flush=True)
    return False


def handle_poll_command(chat_id, cmd, text):
    """Parse /poll, /pollm, /quiz. Always returns True (command consumed)."""
    # Strip the command token (and optional @botname), keep the rest.
    body = re.sub(r"^/\w+(?:@\w+)?\s*", "", text).strip()
    parts = [p.strip() for p in body.split("|") if p.strip()]
    if len(parts) < 3:  # need question + at least 2 options
        send(chat_id, POLL_USAGE)
        return True
    question, raw_opts = parts[0], parts[1:11]  # Telegram caps at 10 options
    if cmd == "quiz":
        correct = [i for i, o in enumerate(raw_opts) if o.startswith("*")]
        if len(correct) != 1:
            send(chat_id, "Quiz needs exactly one *correct answer.\n" + POLL_USAGE)
            return True
        correct_idx = correct[0]
        options = [o[1:].strip() if o.startswith("*") else o for o in raw_opts]
        ok = send_poll(chat_id, question, options, quiz=True,
                       correct_idx=correct_idx)
    else:
        ok = send_poll(chat_id, question, raw_opts, multi=(cmd == "pollm"))
    if not ok:
        send(chat_id, "Couldn't post that poll — try again?")
    return True


DRAFT_PUSH_SECS = 3.0    # min seconds between draft pushes — Telegram 429s
                         # drafts hammered faster than ~1/s
DRAFT_MIN_GROWTH = 40    # only push when the text actually grew this much
DRAFT_MAX = 4096         # sendMessageDraft text cap
TRAILER_LINE_RE = re.compile(r"^\[(buttons|links):.*$", re.M | re.I)
STOP_EVENTS = {}  # chat_id -> threading.Event, set when user hits Stop
_RE_429 = re.compile(r"retry after (\d+)", re.I)
# can_stop/keep_on_stop are Bot API 10.3 (weeks old): probe once, then commit.
# None = untested, True = native stop works, False = omit for this process.
_DRAFT_STOP_OK = None


def _draft_id():
    """Non-zero draft id, unique per reply. Drafts are per-chat, and only one
    stream runs per chat, so a millisecond timestamp is plenty."""
    return (int(time.time() * 1000) & 0x7FFFFFFF) or 1


def draft_update(chat_id, draft_id, text):
    """Push a native streaming draft (private chats only, Bot API 9.3+).
    Empty text shows the native "Thinking…" placeholder. Returns
    (ok, retry_after_secs). 429s are backed off, not hammered — the final
    rich send still lands regardless."""
    global _DRAFT_STOP_OK
    payload = {"chat_id": chat_id, "draft_id": draft_id,
               "text": text[:DRAFT_MAX]}
    if _DRAFT_STOP_OK is not False:
        payload["can_stop"] = True      # 10.3: native Stop button
        payload["keep_on_stop"] = True  # 10.3: keep partial draft on stop
    try:
        r = rich_mod.api_json("sendMessageDraft", payload)
    except Exception as e:
        print(f"[ron_daemon] draft error: {e}", flush=True)
        return False, 0
    if r.get("ok"):
        if _DRAFT_STOP_OK is None:
            _DRAFT_STOP_OK = True
            print("[ron_daemon] draft: native stop supported", flush=True)
        return True, 0
    err = str(r.get("error"))[:160]
    # The first call of a stream always uses empty text (always valid), so a
    # 400 here means the server doesn't know the 10.3 stop params — retry
    # once without them and omit them for the rest of the process.
    if _DRAFT_STOP_OK is None and ("can_stop" in err or "keep_on_stop" in err
                                   or "bad request" in err.lower()):
        _DRAFT_STOP_OK = False
        print(f"[ron_daemon] draft: stop params unsupported ({err}), "
              "retrying without", flush=True)
        payload.pop("can_stop", None)
        payload.pop("keep_on_stop", None)
        try:
            r = rich_mod.api_json("sendMessageDraft", payload)
        except Exception as e:
            print(f"[ron_daemon] draft error: {e}", flush=True)
            return False, 0
        if r.get("ok"):
            return True, 0
        err = str(r.get("error"))[:160]
    m = _RE_429.search(err)
    backoff = int(m.group(1)) if m else 5
    print(f"[ron_daemon] draft not ok (backoff {backoff}s): {err}", flush=True)
    return False, backoff


def send_blocks(chat_id, blocks, reply_to=None, thread_id=None,
                business_connection_id=None):
    """Blocks-form rich send (Bot API 10.3): buttons ride INSIDE the bubble.

    Returns the API response dict ({"ok": ...}). Never raises — failures
    come back as {"ok": False} so callers can degrade gracefully.
    """
    payload = {"chat_id": chat_id, "rich_message": {"blocks": blocks}}
    if reply_to:
        payload["reply_parameters"] = {"message_id": reply_to}
    if thread_id:
        payload["message_thread_id"] = thread_id
    if business_connection_id:
        payload["business_connection_id"] = business_connection_id
    try:
        return rich_mod.api_json("sendRichMessage", payload)
    except Exception as e:
        print(f"[ron_daemon] blocks send error: {e}", flush=True)
        return {"ok": False, "error": str(e)[:150]}


def send_rich_with_kb(chat_id, reply, reply_to=None, thread_id=None,
                      business_connection_id=None):
    """Trailer parsing + IN-BUBBLE rich buttons with safe fallbacks.

    When the reply carries [buttons:]/[links:] trailers, the message goes
    out in blocks form (paragraph/quote/list/code blocks converted from
    the markdown, plus one buttons block per row) so the buttons render
    inside the bubble. Any failure degrades to the classic markdown +
    under-bubble keyboard, then plain text — the reply always lands.
    """
    clean, kb = parse_trailers(reply)
    if not clean:
        clean = "On it ✓"  # trailer-only/empty reply: say something
    rows = kb["inline_keyboard"] if kb else []
    if rows and ron_richblocks is not None:
        try:
            blocks = ron_richblocks.markdown_to_blocks(clean)
            blocks.extend(rich_buttons_blocks(rows))
            r = send_blocks(chat_id, blocks, reply_to, thread_id,
                            business_connection_id)
            if r.get("ok"):
                return r
            print(f"[ron_daemon] blocks send not ok, falling back to "
                  f"keyboard: {str(r)[:120]}", flush=True)
        except Exception as e:
            print(f"[ron_daemon] blocks build/send error: {e}", flush=True)
    # No buttons, no blocks builder, or blocks failed: the classic path.
    for row in rows:  # Bot API 10.3 styled buttons on the keyboard fallback
        for b in row:
            if "callback_data" in b:
                b["style"] = "primary"
    try:
        send_rich(chat_id, clean, kb, reply_to, thread_id,
                  business_connection_id)
    except Exception as e:
        print(f"[ron_daemon] send failed: {e}", flush=True)
        try:
            send(chat_id, strip_markdown(clean), reply_to, thread_id,
                 business_connection_id)
        except Exception:
            pass


# Token economy (2026-09-20, Phase 2): pure acknowledgments never reach the
# AI at all — a canned reply costs zero API budget. Only fires when the
# WHOLE message is an ack (after stripping trailing punctuation); anything
# with real content still goes to the model. Variants rotate per chat so it
# doesn't feel robotic.
_CANNED_REPLIES = {
    "ok": ["Got it 👍", "Noted 👌", "Alright 👍"],
    "okay": ["Got it 👍", "Noted 👌", "Alright 👍"],
    "okk": ["Got it 👍", "Haha, noted 👌"],
    "k": ["👍", "Got it 👌"],
    "kk": ["👍", "Got it 👌"],
    "lol": ["😄", "Haha 😄"],
    "lmao": ["😂", "Haha 😂"],
    "haha": ["😄", "Hehe 😄"],
    "hehe": ["😄", "Hehe 😄"],
    "nice": ["Nice 🙂", "Cool 😎"],
    "cool": ["Cool 😎", "Nice 🙂"],
    "thanks": ["Anytime 🙂", "You got it 👍"],
    "thank you": ["Anytime 🙂", "You got it 👍"],
    "thx": ["Anytime 🙂", "👍"],
    "ty": ["Anytime 🙂", "👍"],
    "bye": ["Later! 👋", "See ya 👋"],
    "good night": ["Good night 🌙", "Sleep well 🌙"],
    "gn": ["Good night 🌙", "🌙"],
    "good morning": ["Morning! ☀️", "Good morning ☀️"],
    "gm": ["Morning! ☀️", "☀️"],
}
_CANNED_PUNCT_RE = None  # compiled lazily (re imported at module top)


def canned_reply(text, chat_id):
    """Return a canned ack for pure-ack messages, else None.

    Zero API cost. Conservative by design: only single acks or pure-emoji
    messages, never anything that could be a real question.
    """
    global _CANNED_PUNCT_RE
    import re as _re
    if _CANNED_PUNCT_RE is None:
        _CANNED_PUNCT_RE = _re.compile(r"[!.,?~…]*$")
    t = (text or "").strip()
    if not t or len(t) > 24:
        return None
    # Pure emoji (no letters/digits at all)?
    if not _re.search(r"[A-Za-z0-9\u00c0-\u024f\u1e00-\u1eff]", t):
        variants = ["👍", "🙂👍", "Gotcha 👍"]
        return variants[abs(hash((str(chat_id), t))) % len(variants)]
    norm = _CANNED_PUNCT_RE.sub("", t.lower()).strip()
    norm = _re.sub(r"\s+", " ", norm)
    variants = _CANNED_REPLIES.get(norm)
    if not variants:
        return None
    return variants[abs(hash((str(chat_id), norm))) % len(variants)]


def _stream_with_draft(chat_id, produce):
    """Shared native-draft streaming core (Bot API sendMessageDraft).

    produce(on_token, stop_event) runs the AI call and returns the full
    reply text. Handles the stop-event slot, throttled draft pushes, and
    the final rich send. Returns the reply text.
    """
    stop_event = threading.Event()
    STOP_EVENTS[chat_id] = stop_event
    buf = []
    draft_id = _draft_id()
    last_push = [0.0]
    last_push_len = [0]
    next_push_ok = [0.0]  # 429 backoff deadline
    # Telegram-side signals go off in the background: the AI call below
    # (the long pole) starts immediately instead of waiting ~0.4s for
    # the typing indicator + empty draft to round-trip first.
    def _kickoff():
        send_typing(chat_id)
        # empty text = native "Thinking…" placeholder, shown immediately
        draft_update(chat_id, draft_id, "")
    kickoff = threading.Thread(target=_kickoff, daemon=True)
    kickoff.start()

    def on_token(tok):
        buf.append(tok)
        cur = "".join(buf)
        now = time.time()
        # live draft update: throttled + only on real growth + past backoff.
        # plain text only — partial markdown mid-generation is not worth
        # the 400s; the final rich send carries the formatting.
        if (len(cur) - last_push_len[0] >= DRAFT_MIN_GROWTH
                and now - last_push[0] >= DRAFT_PUSH_SECS
                and now >= next_push_ok[0]):
            snippet = strip_markdown(strip_trailer_syntax(cur)).strip()
            ok, backoff = draft_update(chat_id, draft_id, snippet)
            if ok:
                last_push_len[0] = len(cur)
                last_push[0] = now
            elif backoff:
                next_push_ok[0] = now + backoff

    try:
        reply = produce(on_token, stop_event)
    finally:
        # Never leave the kickoff thread dangling: the final send below
        # must not race the "Thinking…" draft it was setting up.
        kickoff.join(timeout=10)
        # Only clear our own slot — a newer concurrent stream in this chat
        # may have registered its event after us.
        if STOP_EVENTS.get(chat_id) is stop_event:
            STOP_EVENTS.pop(chat_id, None)

    # short reply (nothing streamed) or post-stream: single rich send.
    # on generation failure the exception propagates and the caller's
    # single-send fallback replies — never an expired preview with nothing.
    send_rich_with_kb(chat_id, reply)
    return reply


def stream_ai_reply(chat_id, user_id, user_name, text):
    """Native draft streaming for 1:1 chats (Bot API sendMessageDraft): the
    reply animates in a draft bubble as tokens arrive (ChatGPT-style),
    throttled to stay under Telegram's rate limits. The client's native Stop
    button halts generation via the stopped_message_generation update.
    Drafts auto-expire; the final sendRichMessage is the persistent reply.
    Falls back to a single send on any failure."""
    reply = _stream_with_draft(
        chat_id,
        lambda on_token, stop_event: ron_ai.chat_stream(
            chat_id, text, user_name, on_token,
            stop_event=stop_event, system_extra=group_context(),
            user_id=user_id, is_private=True))
    handle_group_sends(chat_id, user_id, user_name, reply)
    handle_send_topic(chat_id, user_id, user_name, reply)
    handle_topic_actions(chat_id, user_name, reply)
    # Phase 6: return the reply text (truthy, like the old True) so the
    # caller can store it in the answer cache.
    return reply


def stream_vision_reply(chat_id, user_id, user_name, caption, image_b64):
    """Vision variant of stream_ai_reply: same draft streaming, Gemini
    vision backend. History stores "[photo]" — never image bytes.

    Vision replies skip the group-send/topic-action side effects: the
    vision prompt is memory-blind by construction (no private facts in,
    none can leak out), and bracket text in a photo description must not
    trigger cross-chat actions.
    """
    reply = _stream_with_draft(
        chat_id,
        lambda on_token, stop_event: ron_ai.vision_stream(
            image_b64, caption, on_token, stop_event))
    try:
        ron_ai._save_turn(chat_id, ("[photo] " + (caption or "")).strip(),
                          user_name, reply, user_id)
    except Exception:
        pass
    return reply


# Phase 6 — semantic answer cache helpers. All None-safe: if the cache
# module is unavailable the bot just replies via the AI chain as before.
def _cache_lookup(chat_id, text):
    """Return a cached reply for a semantically equivalent question, or
    None. Never raises — a cache failure must never break a reply."""
    if ron_cache is None:
        return None
    try:
        return ron_cache.lookup(chat_id, text)
    except Exception as e:
        print(f"[ron_daemon] cache lookup failed: {type(e).__name__}",
              flush=True)
        return None


def _cache_store(chat_id, text, reply, cacheable):
    """Store a successful AI reply for future equivalent questions.

    Skipped when: caching was disabled for this call (overshare-approval
    answers), the prompt injected another chat's consented private fact
    (disclosure path bypasses the cache entirely), or the reply is an
    error/degraded notice (only call this on the success paths).
    """
    if ron_cache is None or not cacheable:
        return
    if _consent_disclosed():
        return
    try:
        ron_cache.store(chat_id, text, reply)
    except Exception as e:
        print(f"[ron_daemon] cache store failed: {type(e).__name__}",
              flush=True)


def ai_reply(chat_id, user_id, user_name, text, private=True, reply_to=None,
           thread_id=None, business_connection_id=None, cacheable=True):
    """Run the LLM call off the poll loop so updates keep flowing."""
    def _go():
        cur = threading.current_thread()
        try:
            # Phase 3 degraded mode: every provider is down — don't burn
            # API calls, queue the message and be honest about it.
            if _brain_down():
                _queue_message(chat_id, user_id, user_name, text, reply_to,
                               thread_id, business_connection_id, private)
                _maybe_degraded_notice(chat_id, reply_to, thread_id,
                                       business_connection_id)
                return
            try:
                # Phase 6: semantic answer cache — serve a recent
                # semantically-equivalent answer without burning API
                # tokens. Reset the disclosure flag: _memory_context sets
                # it on this thread when the prompt for the reply below
                # injects another chat's consented private fact, and such
                # replies must never be cached.
                _CONSENT_DISCLOSED.flag = False
                # Light mode (2026-09-20): run lean — cheap models for every
                # message, no draft streaming, daily notice on interaction.
                lm = ron_ai.light_mode_enabled()
                if cacheable:
                    cached = _cache_lookup(chat_id, text)
                    if cached is not None:
                        if lm:
                            cached += ron_ai.light_mode_notice(user_id)
                        send_rich_with_kb(chat_id, cached, reply_to,
                                          thread_id, business_connection_id)
                        handle_group_sends(chat_id, user_id, user_name,
                                           cached)
                        handle_send_topic(chat_id, user_id, user_name,
                                          cached)
                        handle_topic_actions(
                            chat_id, user_name, cached,
                            origin_group_id=None if private else chat_id,
                            origin_thread_id=(None if private
                                              else thread_id))
                        print(f"[ron_daemon] cache hit (chat={chat_id})",
                              flush=True)
                        return
                extra = group_context()
                if not private and _MEM_MODULES:
                    # Phase 4 backstop: the deterministic tripwire catches
                    # most personal questions, but if one slips through the
                    # model must still not answer it publicly.
                    extra += ron_privacy.GROUP_PRIVACY_RULE
                if thread_id and not private:
                    tname = topic_name(chat_id, thread_id)
                    if tname:
                        extra += (f"\nYou are replying inside the '{tname}' topic. "
                                  "When the user says 'this topic', they mean it.")
                # Business account reply: no draft streaming — drafts are a
                # preview in the bot's own chats, but this reply must go out
                # on the business account as one final message.
                # Light mode also skips streaming: single cheap send instead.
                if private and not business_connection_id and not lm:
                    try:
                        reply = stream_ai_reply(chat_id, user_id, user_name,
                                                text)
                        _record_brain_success()
                        # Phase 6: cache the successful reply (before any
                        # light-mode footer) for equivalent future questions.
                        _cache_store(chat_id, text, reply, cacheable)
                        return
                    except Exception as e:
                        emsg = str(e)
                        # Token economy (2026-09-20): the stream attempt
                        # already ran the FULL failover chain inside
                        # ron_ai._request — every provider tried. A
                        # single-send retry would just burn a second full
                        # pass for the same dead end, so skip it and go
                        # straight to the honest public message. (Transient
                        # mid-stream blips still fall through to chat().)
                        if "rate limited" in emsg or "not connected yet" in emsg:
                            _record_brain_failure(emsg)
                            print(f"[ron_daemon] stream exhausted all providers "
                                  f"({emsg[:60]}), skipping single-send retry",
                                  flush=True)
                            if "not connected yet" in emsg:
                                reply = ("My AI brain isn't plugged in yet \u2014 "
                                         "the human's hooking up the API key, one sec. \U0001F527")
                            else:
                                reply = ("Whoa, too many requests at once \u2014 "
                                         "give it a minute and try again. \u23F3")
                            send_rich_with_kb(chat_id, reply, reply_to,
                                              thread_id,
                                              business_connection_id)
                            return
                        if _is_systemic(emsg):
                            _record_brain_failure(emsg)
                        print(f"[ron_daemon] stream failed, single-send fallback: {e}",
                              flush=True)
                # non-streaming path (groups, business): typing fires in the
                # background while the AI call starts immediately.
                _kt = typing_kickoff(chat_id, business_connection_id)
                reply = ron_ai.chat(chat_id, text, user_name,
                                    system_extra=extra, user_id=user_id,
                                    is_private=private)
                _kt.join(timeout=10)
                _record_brain_success()
                # Phase 6: cache the successful reply (before the
                # light-mode footer, which is per-user/per-day).
                _cache_store(chat_id, text, reply, cacheable)
            except Exception as e:
                msg = str(e)
                if _is_systemic(msg):
                    _record_brain_failure(msg)
                if "not connected yet" in msg:
                    reply = ("My AI brain isn't plugged in yet \u2014 "
                             "the human's hooking up the API key, one sec. \U0001F527")
                elif "rate limited" in msg:
                    reply = ("Whoa, too many requests at once \u2014 "
                             "give it a minute and try again. \u23F3")
                else:
                    reply = "Brain glitch, try that again in a bit. \U0001F9E0"
                    print(f"[ron_daemon] ai error: {e}", flush=True)
            if lm:
                # Mention light mode on interaction (once per user per day):
                # users should know the bot is capable of heavier lifting.
                reply += ron_ai.light_mode_notice(user_id)
            send_rich_with_kb(chat_id, reply, reply_to, thread_id,
                              business_connection_id)
            handle_group_sends(chat_id, user_id, user_name, reply)
            handle_send_topic(chat_id, user_id, user_name, reply)
            handle_topic_actions(chat_id, user_name, reply,
                                 origin_group_id=None if private else chat_id,
                                 origin_thread_id=None if private else thread_id)
        finally:
            _untrack(cur)

    t = threading.Thread(target=_go, daemon=True)
    _track(t)
    t.start()


def ai_vision_reply(chat_id, user_id, user_name, caption, file_id,
                    private=True, reply_to=None, thread_id=None,
                    business_connection_id=None):
    """Photo -> vision answer, run off the poll loop like ai_reply.

    The Telegram download happens inside the worker thread so a slow
    fetch never stalls update polling. Quota is recorded only after a
    successful fetch — a failed download never consumes one of the 5.

    Differences from the text path, all deliberate:
    - No semantic cache: image bytes can't be keyed meaningfully, and the
      5-per-6h cap already bounds cost.
    - No degraded queue: image bytes can't be queued and replayed later,
      so a dead vision backend gets an honest message instead.
    - Vision failures never feed the shared brain-failure counter: the
      vision backend (Gemini-only) is independent of the text chain, so a
      vision quota exhaustion must not push text chat into degraded mode.
    - The vision prompt carries no memory/history: it is memory-blind by
      construction, so nothing private can leak through a photo answer.
    """
    def _go():
        cur = threading.current_thread()
        try:
            if ron_vision is None:
                return
            try:
                image_b64 = ron_vision.fetch_photo_b64(file_id)
            except Exception as e:
                print(f"[ron_daemon] photo fetch failed: {e}", flush=True)
                send_rich_with_kb(chat_id, "Couldn't grab that photo — "
                                  "try sending it again. \U0001F4F8",
                                  reply_to, thread_id, business_connection_id)
                return
            ron_vision.record_use(user_id, OWNER_ID)
            lm = ron_ai.light_mode_enabled()
            try:
                if private and not business_connection_id and not lm:
                    stream_vision_reply(chat_id, user_id, user_name,
                                        caption, image_b64)
                else:
                    # groups / business / light mode: single send.
                    _kt = typing_kickoff(chat_id, business_connection_id)
                    try:
                        reply = ron_ai.vision_stream(image_b64, caption)
                    finally:
                        _kt.join(timeout=10)
                    try:
                        ron_ai._save_turn(
                            chat_id, ("[photo] " + (caption or "")).strip(),
                            user_name, reply, user_id)
                    except Exception:
                        pass
                    if lm:
                        reply += ron_ai.light_mode_notice(user_id)
                    send_rich_with_kb(chat_id, reply, reply_to, thread_id,
                                      business_connection_id)
            except Exception as e:
                msg = str(e)
                if "not connected yet" in msg:
                    reply = ("My AI brain isn't plugged in yet \u2014 "
                             "the human's hooking up the API key, one sec. \U0001F527")
                elif "rate limited" in msg:
                    reply = ("Whoa, too many requests at once \u2014 "
                             "give it a minute and try again. \u23F3")
                else:
                    reply = "Couldn't read that image \u2014 try again in a bit. \U0001F9E0"
                    print(f"[ron_daemon] vision error: {e}", flush=True)
                send_rich_with_kb(chat_id, reply, reply_to, thread_id,
                                  business_connection_id)
        finally:
            _untrack(cur)

    t = threading.Thread(target=_go, daemon=True)
    _track(t)
    t.start()


def _entity_text(text, entity):
    """Slice a message entity using Telegram's UTF-16 offsets.

    Bot API entity offset/length are UTF-16 code units; Python string indices
    are code points, so emoji before an entity would shift a naive slice.
    Decode via UTF-16 to stay aligned.
    """
    try:
        u16 = text.encode("utf-16-le")
        start = entity.get("offset", 0) * 2
        return u16[start:start + entity.get("length", 0) * 2].decode("utf-16-le")
    except Exception:
        return ""


def _u16_len(s):
    """Length of s in UTF-16 code units (for entity offset/length fields)."""
    try:
        return len(s.encode("utf-16-le")) // 2
    except Exception:
        return len(s)


def _cb64(s):
    """Truncate to Telegram's 64-byte callback_data limit (UTF-8 safe)."""
    return s.encode("utf-8")[:64].decode("utf-8", "ignore")


def _vision_limit_text(retry_after):
    """Provider-neutral image-cap notice with a human wait estimate."""
    secs = int(retry_after)
    if secs >= 3600:
        wait = f"~{secs // 3600}h {(secs % 3600) // 60}m"
    else:
        wait = f"~{max(1, (secs + 59) // 60)}m"  # round up: "come back in"
    return ("I've hit my image-reading limit for now — send another photo "
            f"in {wait} and I'll take a look. \U0001F4F8")


def _imagen_limit_text(retry_after):
    """Provider-neutral generation-cap notice with a human wait estimate."""
    secs = int(retry_after)
    if secs >= 3600:
        wait = f"~{secs // 3600}h {(secs % 3600) // 60}m"
    else:
        wait = f"~{max(1, (secs + 59) // 60)}m"  # round up: "come back in"
    return ("I've hit my image-generation limit for now — try /imagine "
            f"again in {wait}. \U0001F3A8")


def _imagine_prompt(text):
    """Strip the /imagine (or /imagine@bot) prefix, return the prompt."""
    parts = (text or "").split(None, 1)
    if not parts:
        return ""
    return parts[1].strip() if len(parts) > 1 else ""


def _handle_imagine(chat_id, user_id, user_name, text, is_private,
                    reply_to=None, thread_id=None):
    """/imagine <prompt> -> generated image.

    Rules (mirroring the vision path, all deliberate):
    - Quota is checked BEFORE any provider call: a capped user gets the
      limit notice with zero network burn, and failed generations never
      consume quota (record_use runs only after a successful fetch).
    - The fetch + upload run in a worker thread so slow generation never
      stalls update polling.
    - Maintenance silence for non-owners is enforced at the update level
      (handle_update returns before commands), same as /poll.
    - History stores "[generated image]" + prompt — never image bytes.
    - Failures are provider-neutral: users never hear "pollinations".
    """
    prompt = _imagine_prompt(text)
    if ron_imagen is None:
        send(chat_id, "Image generation isn't available right now.",
             reply_to=reply_to, thread_id=thread_id)
        return True
    if not prompt:
        send(chat_id, "Tell me what to draw — e.g. "
                      "/imagine a red fox in the snow \U0001F3A8",
             reply_to=reply_to, thread_id=thread_id)
        return True
    allowed, retry_after = ron_imagen.check_limit(user_id, OWNER_ID)
    if not allowed:
        send(chat_id, _imagen_limit_text(retry_after),
             reply_to=reply_to, thread_id=thread_id)
        return True
    _ai_imagine_reply(chat_id, user_id, user_name, prompt,
                      private=is_private, reply_to=reply_to,
                      thread_id=thread_id)
    return True


def _ai_imagine_reply(chat_id, user_id, user_name, prompt, private=True,
                      reply_to=None, thread_id=None):
    """Generate + upload an image off the poll loop, like ai_vision_reply."""
    def _go():
        ack_id = None
        try:
            r = send(chat_id, "\U0001F3A8 Generating your image…",
                     reply_to=reply_to, thread_id=thread_id)
            ack_id = (r.get("result") or {}).get("message_id")
        except Exception:
            ack_id = None
        path = None
        try:
            path = ron_imagen.fetch_image(prompt)
        except Exception as e:
            print(f"[ron_daemon] imagine fetch failed: {e}", flush=True)
            if ack_id:
                ron_imagen.delete_message(chat_id, ack_id)
            send_rich_with_kb(
                chat_id,
                "Couldn't generate that image — the image service hiccuped. "
                "Try again in a bit. \U0001F3A8",
                reply_to=reply_to, thread_id=thread_id)
            return
        try:
            # Quota is spent only now — a successful generation.
            ron_imagen.record_use(user_id, OWNER_ID)
            caption = f"\U0001F3A8 {prompt[:200]}"
            up = ron_imagen.send_photo(chat_id, path, caption=caption,
                                       reply_to=reply_to, thread_id=thread_id)
            if ack_id:
                ron_imagen.delete_message(chat_id, ack_id)
            if up.get("ok"):
                try:
                    ron_ai._save_turn(chat_id, f"/imagine {prompt}",
                                      user_name,
                                      f"[generated image] {prompt}", user_id)
                except Exception as e:
                    print(f"[ron_daemon] imagine history save failed: {e}",
                          flush=True)
            else:
                print(f"[ron_daemon] imagine upload failed: "
                      f"{str(up)[:200]}", flush=True)
                send_rich_with_kb(
                    chat_id,
                    "The image was ready but the upload failed — "
                    "try /imagine again. \U0001F3A8",
                    reply_to=reply_to, thread_id=thread_id)
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
    t = threading.Thread(target=_go, daemon=True)
    t.start()


# --- /factcheck (Perplexity, web-grounded) and /think (Gemini, deep) ---
# Explicit brain-switch commands. Both pin a single provider via
# ron_ai.single_shot() — no history, no memory hooks, no cheap routing.
# Quotas are checked BEFORE any provider call (paid/limited APIs); the
# quota is spent only after a successful answer. Worker threads keep slow
# answers off the update poll loop, mirroring /imagine.
_FACTCHECK_MODEL = "sonar-pro"
_FACTCHECK_N = 10            # 10 fact-checks per rolling 6h per user
_FACTCHECK_WINDOW = 6 * 3600
_THINK_MODEL = "gemini-3.8-flash"
_THINK_N = 15                # 15 deep thinks per rolling 6h per user
_THINK_WINDOW = 6 * 3600

_FACTCHECK_SYSTEM = (
    "You are a precise fact-checker with live web search. Given a claim, "
    "verify it against current sources. Reply in this exact shape:\n"
    "**Verdict:** True / False / Misleading / Unverifiable (pick one, one line)\n"
    "Then 2-4 short sentences explaining, with inline citations. "
    "Keep it tight — chat length, not an essay. "
    "Telegram Markdown (**bold**), no HTML."
)
_THINK_SYSTEM = (
    "You are Ron in deep-thinking mode. Think carefully step by step "
    "through the question, then give a thorough, well-reasoned answer. "
    "Keep the reasoning visible but concise, then the conclusion. "
    "Telegram Markdown (**bold**, *italic* sparingly, `code`), no HTML."
)


def _cmd_arg(text):
    """Strip the /command (or /command@bot) prefix, return the argument."""
    parts = (text or "").split(None, 1)
    if not parts:
        return ""
    return parts[1].strip() if len(parts) > 1 else ""


def _cmd_limit_text(cmd, n, window_h, retry_after):
    """Provider-neutral quota notice with a human wait estimate."""
    secs = int(retry_after)
    if secs >= 3600:
        wait = f"~{secs // 3600}h {(secs % 3600) // 60}m"
    else:
        wait = f"~{max(1, (secs + 59) // 60)}m"  # round up: "come back in"
    return (f"I've hit my {cmd} limit for now ({n} per {window_h}h) — "
            f"try again in {wait}.")


def _delete_ack(chat_id, ack_id):
    try:
        api("deleteMessage", {"chat_id": chat_id, "message_id": ack_id})
    except Exception:
        pass


def _handle_factcheck(chat_id, user_id, user_name, text, is_private,
                      reply_to=None, thread_id=None):
    """/factcheck <claim> -> web-grounded verdict via Perplexity."""
    query = _cmd_arg(text)
    if not query:
        send(chat_id,
             "Give me a claim to check — e.g. "
             "/factcheck the Eiffel Tower is 330m tall \U0001F50D",
             reply_to=reply_to, thread_id=thread_id)
        return True
    if ron_cmdquota is None:
        send(chat_id, "Fact-checking isn't available right now.",
             reply_to=reply_to, thread_id=thread_id)
        return True
    if ron_ai._provider_key("perplexity") is None:
        send(chat_id,
             "Fact-checking isn't connected yet — Adwaith needs to add "
             "the Perplexity API key first. \U0001F50D",
             reply_to=reply_to, thread_id=thread_id)
        return True
    allowed, retry_after = ron_cmdquota.check_limit(
        "factcheck", user_id, OWNER_ID, _FACTCHECK_N, _FACTCHECK_WINDOW)
    if not allowed:
        send(chat_id,
             _cmd_limit_text("fact-check", _FACTCHECK_N, 6, retry_after),
             reply_to=reply_to, thread_id=thread_id)
        return True
    _ai_spot_reply("factcheck", chat_id, user_id, user_name, query,
                   "\U0001F50D Fact-checking…",
                   "Couldn't check that right now — the fact-check "
                   "service hiccuped. Try again in a bit. \U0001F50D",
                   reply_to=reply_to, thread_id=thread_id)
    return True


def _handle_think(chat_id, user_id, user_name, text, is_private,
                  reply_to=None, thread_id=None):
    """/think <question> -> deep reasoning via Gemini."""
    query = _cmd_arg(text)
    if not query:
        send(chat_id,
             "Give me something to think about — e.g. "
             "/think why do we dream? \U0001F9E0",
             reply_to=reply_to, thread_id=thread_id)
        return True
    if ron_cmdquota is None:
        send(chat_id, "Deep-thinking isn't available right now.",
             reply_to=reply_to, thread_id=thread_id)
        return True
    if ron_ai._provider_key("gemini") is None:
        send(chat_id,
             "Deep-thinking isn't connected right now. \U0001F9E0",
             reply_to=reply_to, thread_id=thread_id)
        return True
    allowed, retry_after = ron_cmdquota.check_limit(
        "think", user_id, OWNER_ID, _THINK_N, _THINK_WINDOW)
    if not allowed:
        send(chat_id,
             _cmd_limit_text("deep-think", _THINK_N, 6, retry_after),
             reply_to=reply_to, thread_id=thread_id)
        return True
    _ai_spot_reply("think", chat_id, user_id, user_name, query,
                   "\U0001F9E0 Thinking deeply…",
                   "Couldn't think that through right now — try again "
                   "in a bit. \U0001F9E0",
                   reply_to=reply_to, thread_id=thread_id)
    return True


def _ai_spot_reply(scope, chat_id, user_id, user_name, query, ack_text,
                   fail_text, reply_to=None, thread_id=None):
    """Run a pinned-provider command off the poll loop.

    scope "factcheck" -> Perplexity sonar-pro; "think" -> Gemini flash.
    Ack message is deleted before the answer lands, like /imagine.
    """
    def _go():
        ack_id = None
        try:
            r = send(chat_id, ack_text,
                     reply_to=reply_to, thread_id=thread_id)
            ack_id = (r.get("result") or {}).get("message_id")
        except Exception:
            ack_id = None
        try:
            if scope == "factcheck":
                reply = ron_ai.single_shot(
                    "perplexity", _FACTCHECK_MODEL,
                    [{"role": "system", "content": _FACTCHECK_SYSTEM},
                     {"role": "user", "content": query}],
                    timeout=150)
                n, window = _FACTCHECK_N, _FACTCHECK_WINDOW
            else:
                reply = ron_ai.single_shot(
                    "gemini", _THINK_MODEL,
                    [{"role": "system", "content": _THINK_SYSTEM},
                     {"role": "user", "content": query}],
                    timeout=150)
                n, window = _THINK_N, _THINK_WINDOW
        except Exception as e:
            print(f"[ron_daemon] {scope} failed: {e}", flush=True)
            if ack_id:
                _delete_ack(chat_id, ack_id)
            send_rich_with_kb(chat_id, fail_text,
                              reply_to=reply_to, thread_id=thread_id)
            return
        # Quota is spent only now — a successful answer.
        ron_cmdquota.record_use(scope, user_id, OWNER_ID, n, window)
        if ack_id:
            _delete_ack(chat_id, ack_id)
        send_rich_with_kb(chat_id, reply,
                          reply_to=reply_to, thread_id=thread_id)
        try:
            ron_ai._save_turn(chat_id, f"/{scope} {query}", user_name,
                              reply, user_id)
        except Exception as e:
            print(f"[ron_daemon] {scope} history save failed: {e}",
                  flush=True)
    t = threading.Thread(target=_go, daemon=True)
    t.start()


def _handle_photo(msg, chat, frm, user_id, user_name, is_private):
    """Photo message -> vision reply.

    Rules: DMs always; groups only when the bot is mentioned in the
    caption or the photo replies to the bot (same as text). 5 images per
    rolling 6 hours per user, owner exempt. Maintenance mode silences
    everyone but the owner, like text. Only the largest Telegram size is
    read; photo-documents (sent as files) aren't handled in v1.
    """
    chat_id = chat.get("id")
    message_id = msg.get("message_id")
    thread_id = msg.get("message_thread_id")
    caption = (msg.get("caption") or "").strip()
    if ron_vision is None:
        send(chat_id, "I can't read images yet — the human's still "
                      "wiring that up. \U0001F4F8",
             reply_to=message_id, thread_id=thread_id)
        return
    if maintenance_on() and user_id != OWNER_ID:
        return
    if not is_private:
        # Same engagement rule as text: caption mention or reply-to-bot.
        # (Caption entities live in caption_entities, not entities.)
        entities = msg.get("caption_entities", [])
        mentioned = any(
            e.get("type") == "mention"
            and BOT_USERNAME in _entity_text(caption, e)
            for e in entities)
        replied_to_me = (((msg.get("reply_to_message") or {}).get("from")
                          or {}).get("is_bot"))
        if not (mentioned or replied_to_me):
            print(f"[ron_daemon] photo skipped (group, no mention/reply): "
                  f"chat={chat_id} user={user_id}", flush=True)
            return
    # Cap check BEFORE any download/API burn. The owner is exempt.
    allowed, retry_after = ron_vision.check_limit(user_id, OWNER_ID)
    if not allowed:
        send(chat_id, _vision_limit_text(retry_after),
             reply_to=message_id, thread_id=thread_id)
        return
    photos = msg.get("photo") or []
    file_id = (photos[-1] or {}).get("file_id")  # largest size is last
    if not file_id:
        return
    # The download + vision call run in ai_vision_reply's worker thread —
    # the poll loop only did the cheap gating above.
    ai_vision_reply(chat_id, user_id, user_name, caption, file_id,
                    private=is_private, reply_to=message_id,
                    thread_id=thread_id)


def handle_update(upd):
    msg = upd.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return

    # Ephemeral welcome: greet each new human member (only they see it).
    new_members = msg.get("new_chat_members") or []
    if new_members and chat.get("type") in ("group", "supergroup"):
        for nm in new_members:
            if nm.get("is_bot"):
                continue  # skip bots, including Ron itself
            try:
                ephemeral_welcome(chat_id, nm, chat.get("title"))
            except Exception as e:
                print(f"[ron_daemon] ephemeral welcome failed: {e}", flush=True)
            # Phase 4: track them now so disclosure consent gating knows
            # they are present without waiting for them to speak.
            if mem is not None:
                try:
                    mem.note_member(chat_id, nm.get("id", 0),
                                    nm.get("first_name", ""))
                except Exception:
                    pass

    # Learn every group the bot sees so DM-to-group requests can name them.
    if chat.get("type") in ("group", "supergroup"):
        note_group(chat_id, chat.get("title"))
        # Learn forum topics: service messages carry the name, and every
        # message in a forum carries its thread id. (Thread ids can also
        # show up briefly on a group that just had topics disabled — the
        # getChat check below filters those out.)
        tid = msg.get("message_thread_id")
        if tid:
            if forum_status(chat_id) is not False:
                _FORUM_STATUS[str(chat_id)] = (True, time.time())
                note_topic(chat_id, tid)
                ftc = msg.get("forum_topic_created") or {}
                if ftc.get("name") and tid:
                    note_topic(chat_id, tid, name=ftc["name"])
                fte = msg.get("forum_topic_edited") or {}
                if fte.get("name") and tid:
                    note_topic(chat_id, tid, name=fte["name"])
                if "forum_topic_closed" in msg and tid:
                    note_topic(chat_id, tid, closed=True)
                if "forum_topic_reopened" in msg and tid:
                    note_topic(chat_id, tid, closed=False)
            else:
                # Thread id on a group that isn't a forum — transition
                # artifact; drop any stale topics we remembered here.
                purge_topics(chat_id)
        else:
            # No thread info on this message — if we remember topics here,
            # double-check the group is still a forum, and forget them if not.
            if load_known_topics().get(str(chat_id)):
                if forum_status(chat_id) is False:
                    purge_topics(chat_id)

    frm = msg.get("from") or {}
    user_id = frm.get("id", 0)

    text = msg.get("text", "").strip()
    photos = msg.get("photo") or []
    if not text and not photos:
        return
    user_name = frm.get("first_name", "there")
    is_private = chat.get("type") == "private"

    cmd = parse_command(text)
    # Maintenance mode: everyone except the owner is ignored (silently —
    # no AI calls, no replies). /start still reaches handle_command so
    # non-owners get the "taking a break" notice and the owner can wake up.
    if maintenance_on() and user_id != OWNER_ID and cmd != "start":
        return
    # Photos (no text): vision path. Caption, if any, is the question.
    if photos and not text:
        print(f"[ron_daemon] photo msg: chat={chat_id} user={user_id} "
              f"private={is_private} caption={bool(msg.get('caption'))}",
              flush=True)
        _handle_photo(msg, chat, frm, user_id, user_name, is_private)
        return
    if msg.get("document") and msg["document"].get("mime_type", "").startswith("image/"):
        # Photo sent as a file: not supported in v1 — say so instead of
        # going silent, or the user thinks the bot is broken.
        print(f"[ron_daemon] photo-as-document ignored: chat={chat_id} user={user_id}",
              flush=True)
        if not (maintenance_on() and user_id != OWNER_ID):
            send(chat_id, "I can only read photos sent as photos \U0001F4F8 — "
                          "not as files. Resend it as a photo and I'll take a look.",
                 reply_to=msg.get("message_id"), thread_id=msg.get("message_thread_id"))
        return
    if cmd and handle_command(chat_id, user_id, cmd, is_private, text,
                               user_name,
                               reply_to=msg.get("message_id"),
                               thread_id=msg.get("message_thread_id")):
        return

    # Phase 4 (silent): track group membership and check for disclosure
    # triggers. Nothing here posts in the group — approval asks and their
    # answers go via private DM only (a "yes" typed in the group is
    # ambiguous chat, never consent).
    if mem is not None and not is_private:
        mem.note_member(chat_id, user_id, user_name)
        _maybe_trigger_disclosures(chat_id, text, msg.get("message_id"),
                                   chat.get("title") or "this group",
                                   allow_disclose=False)

    if is_private:
        # Phase 4: DM answers to pending approval asks, then natural
        # memory commands ("remember this ...", "forget ...").
        if mem is not None and _resolve_dm_approval(user_id, text):
            return
        if mem is not None and _handle_memory_nl(chat_id, user_id,
                                                  user_name, text):
            return
        canned = canned_reply(text, chat_id)
        if canned is not None:
            # Zero-cost ack: no API call, still saved to history for context.
            ron_ai._save_turn(chat_id, text, user_name, canned, user_id)
            send_rich_with_kb(chat_id, canned)
            return
        ai_reply(chat_id, user_id, user_name, text)
        return

    # Groups: only when mentioned or replied to.
    # (Entity offsets are UTF-16 code units — _entity_text slices accordingly
    # so emoji earlier in the message can't shift the mention out of reach.)
    mentioned = any(
        e.get("type") == "mention" and BOT_USERNAME in _entity_text(text, e)
        for e in msg.get("entities", [])
    )
    replied_to_me = ((msg.get("reply_to_message") or {}).get("from") or {}).get("is_bot")
    if mentioned or replied_to_me:
        clean = text.replace("@" + BOT_USERNAME, "").strip()
        # Phase 4: natural memory commands first.
        if mem is not None and _handle_memory_nl(chat_id, user_id,
                                                  user_name, clean or text):
            return
        # Phase 4 oversharing guard: Adwaith asking something personal in a
        # group gets a private DM approval ask — never answered publicly
        # without his explicit Yes.
        if (user_id == OWNER_ID
                and _MEM_MODULES and ron_privacy.looks_personal(clean or text)
                and _start_overshare_approval(
                    chat_id, user_id, user_name, clean or text,
                    msg.get("message_id"), chat.get("title") or "this group")):
            return
        # The bot is engaged: consented facts may now disclose.
        if mem is not None:
            _maybe_trigger_disclosures(chat_id, clean or text,
                                       msg.get("message_id"),
                                       chat.get("title") or "this group",
                                       allow_disclose=True)
        # Thread the reply so it's clear who Ron is answering.
        canned = canned_reply(clean or text, chat_id)
        if canned is not None:
            # Zero-cost ack: no API call, still saved to history for context.
            ron_ai._save_turn(chat_id, clean or text, user_name, canned,
                              user_id)
            send_rich_with_kb(chat_id, canned,
                              reply_to=msg.get("message_id"),
                              thread_id=msg.get("message_thread_id"))
            return
        ai_reply(chat_id, user_id, user_name, clean or text, private=False,
                 reply_to=msg.get("message_id"),
                 thread_id=msg.get("message_thread_id"))


def handle_callback(query):
    """Quick-reply buttons: tapping one re-sends its label as the user's message.
    Every query is answered exactly once — BotFather alerts on unanswered ones.
    (Stream Stop is now the client's native draft button; the retired inline
    "stop" callback still lands here and is answered harmlessly.)"""
    frm = query.get("from") or {}
    data = query.get("data", "")
    try:
        api("answerCallbackQuery", {"callback_query_id": query["id"]})
    except Exception:
        pass
    # Maintenance mode covers button taps too: text messages are gated in
    # handle_update, but callback queries arrive here directly. Non-owners
    # get silence (the /start command still explains the break).
    if maintenance_on() and frm.get("id") != OWNER_ID:
        return
    if data == "demo_more":
        msg = query.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            try:
                rich_mod.api_json("sendRichMessage", {
                    "chat_id": chat.get("id"),
                    "rich_message": {"markdown":
                        "🎲 *Spoiler quiz*\n"
                        "What has keys but can't open locks?\n\n"
                        "Tap to reveal: ||a piano|| 🎹\n\n"
                        "Spoilers also work mid-sentence: the treasure was "
                        "||under the old oak|| all along."},
                })
            except Exception:
                pass
        return
    if not data.startswith("q:"):
        # Phase 4: private-DM approval buttons (mem:go:<pid> / mem:no:<pid>).
        if _MEM_MODULES:
            parsed = ron_privacy.parse_memory_callback(data)
            if parsed:
                _resolve_callback_approval(query, parsed[0], parsed[1])
        return
    msg = query.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    frm = query.get("from") or {}
    if chat_id:
        # Button-mash throttle: every tap re-sends the label as a message,
        # so a user hammering a button would burn one AI call per tap and
        # flood the chat on degraded-queue replay. One tap per user+button
        # per window; extras are dropped silently.
        tap_key = (chat_id, frm.get("id"), data)
        now = time.time()
        with _TAP_LOCK:
            last = _tap_ts.get(tap_key, 0)
            _tap_ts[tap_key] = now
            if len(_tap_ts) > 2000:
                _tap_ts.clear()
        if now - last < _TAP_WINDOW:
            print(f"[ron_daemon] button-mash dropped (chat={chat_id} "
                  f"user={frm.get('id')})", flush=True)
            return
        ai_reply(chat_id, frm.get("id", 0), frm.get("first_name", "there"),
                 data[2:], private=(chat.get("type") == "private"),
                 thread_id=(msg.get("message_thread_id")))


def ephemeral_welcome(chat_id, user, group_title=None):
    """Ephemeral welcome — visible only to the new joiner, zero group spam."""
    name = user.get("first_name", "there")
    if str(chat_id) == PLAYGROUND:
        text = (
            f"Welcome to Muse Playground, {name}! \U0001F389\n\n"
            "Unofficial hangout for Muse users. Drop a message showing what "
            "your agent can do \u2014 prompts, tips, builds, experiments.\n\n"
            "Be kind, no spam. Not affiliated with Meta. Have fun!"
        )
    else:
        text = (
            f"Welcome to {group_title or 'the group'}, {name}! \U0001F389\n\n"
            "I'm Ron, the assistant here. Mention me if you need anything."
        )
    # NOTE: must go over JSON — the urlencode api() mangles nested dicts
    # (Telegram 400s "can't parse EphemeralMessageParameters JSON object").
    return rich_mod.api_json("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "ephemeral_message_parameters": {"receiver_user_id": user["id"]},
    })


def load_offset():
    try:
        with open(OFFSET_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return 0


def save_offset(offset):
    try:
        tmp = OFFSET_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write(str(offset))
        os.replace(tmp, OFFSET_FILE)
    except Exception as e:
        print(f"[ron_daemon] save offset error: {e}", flush=True)


def main():
    offset = load_offset()
    print(f"[ron_daemon] starting, offset={offset}", flush=True)
    print(f"[ron_daemon] light mode "
          f"{'ON (cheap routing, no streaming, daily notice)' if ron_ai.light_mode_enabled() else 'off'}",
          flush=True)
    # Phase 4: memory store (dual-write + recall hooks + approval sweeper).
    init_memory()
    # Phase 3: heartbeat for the watchdog, startup provider health ping,
    # and the degraded-mode recovery worker.
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    threading.Thread(target=_health_ping, daemon=True).start()
    threading.Thread(target=_recovery_loop, daemon=True).start()
    while not _SHUTDOWN.is_set():
        try:
            r = api("getUpdates", {"offset": offset, "timeout": 50,
                                   "allowed_updates": ["message", "callback_query",
                                                       "stopped_message_generation",
                                                       "business_connection",
                                                       "business_message",
                                                       "edited_business_message",
                                                       "deleted_business_messages"]}, timeout=65)
            if not r.get("ok"):
                print(f"[ron_daemon] getUpdates not ok: {str(r)[:150]}",
                      flush=True)
                time.sleep(5)
                continue
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    if "callback_query" in upd:
                        handle_callback(upd["callback_query"])
                    elif "business_connection" in upd:
                        handle_business_connection(upd["business_connection"])
                    elif "business_message" in upd:
                        handle_business_message(upd["business_message"])
                    elif "edited_business_message" in upd:
                        # Edits in business chats: nothing to redo — log only.
                        ebm = upd["edited_business_message"] or {}
                        print(f"[ron_daemon] edited business message ignored "
                              f"(conn={ebm.get('business_connection_id')})",
                              flush=True)
                    elif "deleted_business_messages" in upd:
                        dbm = upd["deleted_business_messages"] or {}
                        print(f"[ron_daemon] deleted business messages ignored "
                              f"(conn={dbm.get('business_connection_id')}, "
                              f"n={len(dbm.get('message_ids') or [])})",
                              flush=True)
                    elif "stopped_message_generation" in upd:
                        # 10.3 native Stop button: halt the in-flight stream
                        smg = upd["stopped_message_generation"] or {}
                        cid = ((smg.get("chat") or {}).get("id")
                               or ((smg.get("message") or {}).get("chat")
                                   or {}).get("id"))
                        ev = STOP_EVENTS.get(cid)
                        if ev is not None:
                            print(f"[ron_daemon] stop pressed, chat={cid}",
                                  flush=True)
                            ev.set()
                    else:
                        handle_update(upd)
                except Exception as e:
                    print(f"[ron_daemon] update error: {e}", flush=True)
                # Ack each update as handled (even on error, so a poison
                # update can't wedge the loop) — a crash now loses at most
                # one update instead of re-sending a whole batch.
                save_offset(offset)
        except Exception as e:
            print(f"[ron_daemon] loop error: {e}", flush=True)
            time.sleep(5)
    # Drain in-flight replies before exiting (max ~25s).
    deadline = time.time() + 25
    while time.time() < deadline:
        with _INFLIGHT_LOCK:
            remaining = len(_INFLIGHT)
        if remaining == 0:
            break
        time.sleep(0.5)
    print(f"[ron_daemon] stopped (in-flight remaining: {remaining})", flush=True)


if __name__ == "__main__":
    main()
