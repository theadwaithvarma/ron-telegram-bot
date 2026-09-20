#!/usr/bin/env python3
"""Batch-delete messages (Bot API 7.0 deleteMessages).

Moderation helper for the Playground: wipe a spam burst in one API call.

Usage:
    delete_messages.py <chat_id> <msg_id> [<msg_id> ...]
"""
import os
import sys
import json
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import BOT_TOKEN, read_json_response


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        sys.exit(2)
    chat_id = argv[1]
    ids = [int(x) for x in argv[2:]]
    payload = json.dumps({"chat_id": chat_id, "message_ids": ids}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessages",
        data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            print(json.dumps(read_json_response(resp)))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)[:200]}))


if __name__ == "__main__":
    main(sys.argv)
