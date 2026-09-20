#!/usr/bin/env python3
"""Send Telegram Bot API 10.2+ ephemeral messages.

Ephemeral messages live inside a group/supergroup but are visible ONLY to
one target user — perfect for welcomes, whispers, per-user errors.
They carry message_id 0; edits/deletes go through editEphemeralMessage*
with chat_id + receiver_user_id + ephemeral_message_id.

Usage:
    ephemeral_send.py <chat_id> <user_id> <text...>
    ephemeral_send.py <chat_id> <user_id> --rich --html "<h3>Hi</h3>..."
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import BOT_TOKEN
import http_pool


def api(method, payload):
    """Telegram API call over the keep-alive connection pool (saves a
    fresh proxy CONNECT + TLS handshake per ephemeral send)."""
    data = json.dumps(payload).encode()
    try:
        status, raw = http_pool.pool_for("api.telegram.org").post(
            f"/bot{BOT_TOKEN}/{method}", data, "application/json", 30)
    except Exception as e:
        return {"ok": False, "error": str(e)[:250]}
    if status != 200:
        return {"ok": False, "error":
                f"HTTP {status}: "
                f"{raw[:120].decode('utf-8', 'replace')}"[:250]}
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"bad JSON: {e}"[:250]}


def ephemeral_send(chat_id, user_id, text, rich=False, fmt="markdown",
                   **kwargs):
    """Send an ephemeral message to user_id inside chat_id."""
    params = {"chat_id": chat_id,
              "ephemeral_message_parameters": {"receiver_user_id": user_id}}
    if rich:
        return api("sendRichMessage",
                   {**params, "rich_message": {fmt: text}, **kwargs})
    params["text"] = text
    return api("sendMessage", {**params, **kwargs})


def ephemeral_edit_text(chat_id, user_id, ephemeral_message_id, text):
    return api("editEphemeralMessageText", {
        "chat_id": chat_id,
        "receiver_user_id": user_id,
        "ephemeral_message_id": ephemeral_message_id,
        "text": text,
    })


def ephemeral_delete(chat_id, user_id, ephemeral_message_id):
    return api("deleteEphemeralMessage", {
        "chat_id": chat_id,
        "receiver_user_id": user_id,
        "ephemeral_message_id": ephemeral_message_id,
    })


def main(argv):
    rich = "--rich" in argv
    fmt = "html" if "--html" in argv else "markdown"
    args = [a for a in argv[1:] if not a.startswith("--")]
    if len(args) < 3:
        print(__doc__)
        sys.exit(2)
    chat_id, user_id = args[0], int(args[1])
    text = " ".join(args[2:])
    print(json.dumps(ephemeral_send(chat_id, user_id, text.strip(),
                                    rich=rich, fmt=fmt)))


if __name__ == "__main__":
    main(sys.argv)
