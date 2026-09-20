#!/usr/bin/env python3
"""Stream a message to a Telegram 1:1 chat like ChatGPT does.

Uses sendMessageDraft for progressive partial-text updates (animated in the
client), then finalizes with a normal sendMessage. Falls back to a plain
sendMessage if drafts fail.

Usage:
    stream_send.py <chat_id> <text...> [--delay 0.7] [--no-stream]
    echo "long text" | stream_send.py <chat_id>

Short messages (<= STREAM_MIN_CHARS) skip streaming and send instantly.
"""
import os
import sys
import time
import urllib.request
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import BOT_TOKEN, read_json_response

STREAM_MIN_CHARS = 150
MAX_CHUNKS = 8


def api(method, params):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return read_json_response(resp)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def chunk_prefixes(text, n):
    """Split text into n progressive prefixes at word boundaries."""
    words = text.split(" ")
    if len(words) <= n:
        return [" ".join(words[: i + 1]) for i in range(len(words))]
    per = len(words) / n
    out = []
    for i in range(1, n + 1):
        cut = max(1, round(per * i))
        out.append(" ".join(words[:cut]))
    # ensure the last prefix is the full text
    out[-1] = text
    # drop duplicates (short texts)
    dedup = []
    for p in out:
        if not dedup or p != dedup[-1]:
            dedup.append(p)
    return dedup


def stream_send(chat_id, text, delay=0.7, draft_id=99001, force=False):
    text = text.strip()
    if not text:
        return {"ok": False, "error": "empty text"}

    if not force and len(text) <= STREAM_MIN_CHARS:
        return api("sendMessage", {"chat_id": chat_id, "text": text})

    n = min(MAX_CHUNKS, max(3, len(text) // 120))
    prefixes = chunk_prefixes(text, n)

    draft_ok = True
    for prefix in prefixes[:-1]:
        r = api("sendMessageDraft", {
            "chat_id": chat_id,
            "draft_id": draft_id,
            "text": prefix,
        })
        if not r.get("ok"):
            draft_ok = False
            break
        time.sleep(delay)

    # Finalize: full text as a real message (draft is only a 30s preview).
    final = api("sendMessage", {"chat_id": chat_id, "text": text})
    final["streamed"] = draft_ok
    return final


def main(argv):
    delay = 0.7
    force = False
    args = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--delay" and i + 1 < len(argv):
            delay = float(argv[i + 1])
            i += 2
            continue
        if a.startswith("--delay="):
            delay = float(a.split("=", 1)[1])
            i += 1
            continue
        if a == "--force":
            force = True
            i += 1
            continue
        if a.startswith("--"):
            i += 1
            continue
        args.append(a)
        i += 1
    if len(args) < 1:
        print(__doc__)
        sys.exit(2)
    chat_id = args[0]
    if len(args) > 1:
        text = " ".join(args[1:])
    else:
        text = sys.stdin.read()
    import json
    print(json.dumps(stream_send(chat_id, text, delay=delay, force=force)))


if __name__ == "__main__":
    main(sys.argv)
