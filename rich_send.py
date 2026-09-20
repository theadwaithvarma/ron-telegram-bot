#!/usr/bin/env python3
"""Send Telegram Bot API 10.1 rich messages (sendRichMessage).

Supports headings, tables, lists, quotes, code blocks, collapsible blocks,
inline media, math, etc. — via markdown or html. Up to 32,768 chars.
Works in private chats, groups, and channels.

Usage:
    rich_send.py <chat_id> [--html|--markdown] <text...>
    echo "# Title" | rich_send.py <chat_id> --markdown
"""
import os
import sys
import json
import urllib.request
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import BOT_TOKEN, read_json_response


def api_json(method, payload):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return read_json_response(resp)
    except Exception as e:
        body = ""
        try:
            body = e.read().decode()[:300]  # noqa
        except Exception:
            pass
        return {"ok": False, "error": f"{e} {body}"[:300]}


def rich_send(chat_id, text, fmt="markdown", **kwargs):
    return api_json("sendRichMessage", {
        "chat_id": chat_id,
        "rich_message": {fmt: text},
        **kwargs,
    })


def rich_stream(chat_id, text, fmt="markdown", draft_id=99001, delay=0.7,
                chunks=6):
    """Stream a rich message in a private chat via sendRichMessageDraft."""
    import time
    words = text.split(" ")
    per = max(1, len(words) / chunks)
    prefixes = []
    for i in range(1, chunks + 1):
        cut = max(1, round(per * i))
        prefixes.append(" ".join(words[:cut]))
    prefixes[-1] = text
    for prefix in prefixes[:-1]:
        r = api_json("sendRichMessageDraft", {
            "chat_id": chat_id,
            "draft_id": draft_id,
            "rich_message": {fmt: prefix},
        })
        if not r.get("ok"):
            return {"ok": False, "error": f"draft failed: {r}"[:200]}
        time.sleep(delay)
    return rich_send(chat_id, text, fmt=fmt)


def main(argv):
    fmt = "markdown"
    args = []
    for a in argv[1:]:
        if a == "--html":
            fmt = "html"
        elif a == "--markdown":
            fmt = "markdown"
        else:
            args.append(a)
    if not args:
        print(__doc__)
        sys.exit(2)
    chat_id = args[0]
    text = " ".join(args[1:]) if len(args) > 1 else sys.stdin.read()
    print(json.dumps(rich_send(chat_id, text.strip(), fmt=fmt)))


if __name__ == "__main__":
    main(sys.argv)
