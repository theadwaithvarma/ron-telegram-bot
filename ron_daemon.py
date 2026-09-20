#!/usr/bin/env python3
"""Ron AI daemon — conversational Telegram bot, long-polling getUpdates.

Replies to everyone: DMs from any user, and group messages that mention
the bot or reply to it. (Owner-only pause was lifted 2026-09-20.)
Replies use ron_ai.py (Groq backend), streaming live via Telegram's native
message drafts (sendMessageDraft) for long 1:1 answers, finalized as native
rich messages (sendRichMessage) with contextual quick-reply buttons.

Run: copy .env.example to .env, fill it in, then
     nohup python3 ron_daemon.py >ron_daemon.log 2>&1 &
State: offset persisted next to the script (ron_daemon.offset)
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ron_ai
import rich_send as rich_mod

from config import (
    BASE_DIR,
    BOT_TOKEN,
    BOT_USERNAME,
    OWNER_ID,
    read_json_response,
    telegram_api_base,
)

OFFSET_FILE = str(BASE_DIR / "ron_daemon.offset")
BASE = telegram_api_base()
PLAYGROUND = "-1003920795086"

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


def _atomic_write_json(path, obj):
    """Write JSON via tmp+rename so a crash mid-write never leaves a torn file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _track(t):
    with _INFLIGHT_LOCK:
        _INFLIGHT.add(t)


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
    url = f"{BASE}/{method}"
    data = urllib.parse.urlencode(params or {}).encode() if params else None
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return read_json_response(resp)
    except Exception as e:
        return {"ok": False, "error": str(e)[:150]}


def send(chat_id, text, reply_to=None, thread_id=None):
    params = {"chat_id": chat_id, "text": text}
    if reply_to:
        # api() urlencodes, so nested objects must be pre-serialized to JSON
        params["reply_parameters"] = json.dumps({"message_id": reply_to})
    if thread_id:
        params["message_thread_id"] = thread_id
    return api("sendMessage", params)


def send_markup(chat_id, text, reply_markup, reply_to=None):
    params = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": json.dumps(reply_markup),
    }
    if reply_to:
        params["reply_parameters"] = json.dumps({"message_id": reply_to})
    return api("sendMessage", params)


def send_typing(chat_id):
    """Show 'typing...' at the top of the chat. Telegram expires it after
    ~5s, so long generations must resend it periodically."""
    try:
        api("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass


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
KNOWN_GROUPS_FILE = str(BASE_DIR / "known_groups.json")

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
KNOWN_TOPICS_FILE = str(BASE_DIR / "known_topics.json")

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


def parse_trailers(reply):
    """Pull [buttons: ...] / [links: ...] trailer lines into an inline keyboard."""
    btns = (BTN_RE.search(reply) or BTN_TAIL_RE.search(reply) or [None, None])[1]
    links = (LINK_RE.search(reply) or LINK_TAIL_RE.search(reply) or [None, None])[1]
    clean = strip_trailer_syntax(reply)
    kb = []
    if btns:
        # callback_data is capped at 64 bytes by Telegram — truncate by bytes,
        # not chars, so multibyte labels can't produce BUTTON_DATA_INVALID.
        row = [{"text": b[:40], "callback_data": _cb64("q:" + b)}
               for b in (x.strip() for x in btns.split("|")) if b][:8]
        if row:
            kb.append(row)
    if links:
        row = []
        for part in (x.strip() for x in links.split("|") if x.strip()):
            if "->" not in part:
                continue
            label, url = (x.strip() for x in part.split("->", 1))
            if url.startswith("http"):
                row.append({"text": label[:40], "url": url})
        if row:
            kb.append(row[:8])
    return clean, {"inline_keyboard": kb} if kb else None


def strip_markdown(t):
    """Degrade rich Markdown to plain text for the no-format fallback."""
    t = re.sub(r"```\w*\n?", "", t)
    t = re.sub(r"[*_`~]", "", t)
    t = re.sub(r"^#{1,6}\s+", "", t, flags=re.M)
    t = re.sub(r"^>\s?", "", t, flags=re.M)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    return t.strip()


def send_rich(chat_id, text, reply_markup=None, reply_to=None, thread_id=None):
    """Native rich message (Bot API 10.1); falls back to plain text."""
    rp = {"reply_parameters": {"message_id": reply_to}} if reply_to else {}
    try:
        kw = {"reply_markup": reply_markup} if reply_markup else {}
        if thread_id:
            kw["message_thread_id"] = thread_id
        r = rich_mod.rich_send(chat_id, text, fmt="markdown", **kw, **rp)
        if r.get("ok"):
            return r
        print(f"[ron_daemon] rich send not ok: {str(r)[:120]}", flush=True)
    except Exception as e:
        print(f"[ron_daemon] rich send error: {e}", flush=True)
    plain = strip_markdown(text)
    if reply_markup:
        r2 = send_markup(chat_id, plain, reply_markup, reply_to)
        if r2.get("ok"):
            return r2
        # Bad keyboard (oversized button etc.) — drop it, keep the text.
        print(f"[ron_daemon] markup send not ok, retrying plain: "
              f"{str(r2)[:120]}", flush=True)
    return send(chat_id, plain, reply_to, thread_id)


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


def handle_command(chat_id, user_id, cmd, is_private, text=""):
    if cmd == "ping":
        send(chat_id, "\U0001F3D3 pong \u2014 alive and polling.")
        return True
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
    return False


# BOT_USERNAME comes from config (BOT_USERNAME env, defaults to the bot's
# handle); commands like /poll@otherbot are ignored via parse_command.


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


def send_rich_with_kb(chat_id, reply, reply_to=None, thread_id=None):
    """Trailer parsing + styled buttons + rich send with safe fallbacks."""
    clean, kb = parse_trailers(reply)
    if not clean:
        clean = "On it ✓"  # trailer-only/empty reply: say something
    if kb:  # Bot API 10.3 styled buttons
        for row in kb["inline_keyboard"]:
            for b in row:
                if "callback_data" in b:
                    b["style"] = "primary"
    try:
        send_rich(chat_id, clean, kb, reply_to, thread_id)
    except Exception as e:
        print(f"[ron_daemon] send failed: {e}", flush=True)
        try:
            send(chat_id, strip_markdown(clean), reply_to, thread_id)
        except Exception:
            pass


def stream_ai_reply(chat_id, user_id, user_name, text):
    """Native draft streaming for 1:1 chats (Bot API sendMessageDraft): the
    reply animates in a draft bubble as tokens arrive (ChatGPT-style),
    throttled to stay under Telegram's rate limits. The client's native Stop
    button halts generation via the stopped_message_generation update.
    Drafts auto-expire; the final sendRichMessage is the persistent reply.
    Falls back to a single send on any failure."""
    stop_event = threading.Event()
    STOP_EVENTS[chat_id] = stop_event
    buf = []
    draft_id = _draft_id()
    last_push = [0.0]
    last_push_len = [0]
    next_push_ok = [0.0]  # 429 backoff deadline
    send_typing(chat_id)
    # empty text = native "Thinking…" placeholder, shown immediately
    draft_update(chat_id, draft_id, "")

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
        reply = ron_ai.chat_stream(chat_id, text, user_name, on_token,
                                   stop_event=stop_event,
                                   system_extra=group_context())
    finally:
        # Only clear our own slot — a newer concurrent stream in this chat
        # may have registered its event after us.
        if STOP_EVENTS.get(chat_id) is stop_event:
            STOP_EVENTS.pop(chat_id, None)

    # short reply (nothing streamed) or post-stream: single rich send.
    # on generation failure the exception propagates and the caller's
    # single-send fallback replies — never an expired preview with nothing.
    send_rich_with_kb(chat_id, reply)
    handle_group_sends(chat_id, user_id, user_name, reply)
    handle_send_topic(chat_id, user_id, user_name, reply)
    handle_topic_actions(chat_id, user_name, reply)
    return True


def ai_reply(chat_id, user_id, user_name, text, private=True, reply_to=None,
           thread_id=None):
    """Run the LLM call off the poll loop so updates keep flowing."""
    def _go():
        cur = threading.current_thread()
        try:
            try:
                extra = group_context()
                if thread_id and not private:
                    tname = topic_name(chat_id, thread_id)
                    if tname:
                        extra += (f"\nYou are replying inside the '{tname}' topic. "
                                  "When the user says 'this topic', they mean it.")
                if private:
                    try:
                        stream_ai_reply(chat_id, user_id, user_name, text)
                        return
                    except Exception as e:
                        print(f"[ron_daemon] stream failed, single-send fallback: {e}",
                              flush=True)
                # non-streaming path (groups): show typing while generating
                send_typing(chat_id)
                reply = ron_ai.chat(chat_id, text, user_name,
                                    system_extra=extra)
            except Exception as e:
                msg = str(e)
                if "not connected yet" in msg:
                    reply = ("My AI brain isn't plugged in yet \u2014 "
                             "the human's hooking up the API key, one sec. \U0001F527")
                elif "rate limited" in msg:
                    reply = ("Groq's rate-limiting me right now \u2014 "
                             "give it a minute and try again. \u23F3")
                else:
                    reply = "Brain glitch, try that again in a bit. \U0001F9E0"
                    print(f"[ron_daemon] ai error: {e}", flush=True)
            send_rich_with_kb(chat_id, reply, reply_to, thread_id)
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
    if not text:
        return
    user_name = frm.get("first_name", "there")
    is_private = chat.get("type") == "private"

    cmd = parse_command(text)
    if cmd and handle_command(chat_id, user_id, cmd, is_private, text):
        return

    if is_private:
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
        # Thread the reply so it's clear who Ron is answering.
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
        return
    msg = query.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    frm = query.get("from") or {}
    if chat_id:
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
    while not _SHUTDOWN.is_set():
        try:
            r = api("getUpdates", {"offset": offset, "timeout": 50,
                                   "allowed_updates": ["message", "callback_query",
                                                       "stopped_message_generation"]}, timeout=65)
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
