#!/usr/bin/env python3
"""Send a Telegram DM to the bot owner.

Used by ensure_up.sh for up/down transition alerts. Uses the same
dynamic credential surrogate as the daemon (sent literally and unquoted
in the URL path, per the egress proxy's requirement). Reads proxy
settings from the persistent env file so it works in bare cron context.
"""
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
from dynamic_credentials import dynamic_credential_entry  # noqa: E402

OWNER_ID = 6819786239
ENV_FILE = os.path.expanduser("~/.config/ron/env")


def _load_proxy_env():
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line.startswith(("https_proxy=", "HTTPS_PROXY=")):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k, v)
    except OSError:
        pass


def main():
    text = sys.argv[1] if len(sys.argv) > 1 else "(no message)"
    _load_proxy_env()
    try:
        surrogate = str(
            dynamic_credential_entry("custom.telegram")["surrogate"]).strip()
    except Exception as e:
        print(f"alert failed: no credential ({e})")
        return 1
    url = f"https://api.telegram.org/bot{surrogate}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": OWNER_ID, "text": text}).encode()
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, data=data), timeout=30) as r:
            code = r.status
    except Exception as e:
        print(f"alert failed: {e}")
        return 1
    print(f"alert sent (http {code})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
