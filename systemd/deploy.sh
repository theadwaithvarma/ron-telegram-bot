#!/bin/bash
# deploy.sh — crash-neutral deploy: repo -> live runtime + daemon restart.
#
# Why this exists: the watchdog counts every unexpected daemon death as a
# "crash" and enters exponential backoff (up to ~49 min). A bare
# `kill <daemon-pid>` after copying new files looks exactly like a crash:
# on 2026-09-21 two deploys ~10 min apart pushed consecutive_crashes to 4,
# the watchdog backed off for 49 minutes, and the 5-minute self-heal cron
# fired a false "Ron is down and auto-recovery failed" alert to the owner
# for what was really a routine deploy. ALWAYS deploy through this script,
# never with a bare `kill`.
#
# What it does:
#   1. copies runtime .py files + systemd scripts from repo to live
#      (never credentials, state, logs, or ron_ai.json)
#   2. resets the watchdog's crash counters so the restart below is not
#      misread as a crash (deploys are operator-initiated, not failures)
#   3. SIGTERMs the daemon; the watchdog relaunches it on its next 60s
#      check cycle (allow up to ~90s)
#   4. verifies a fresh heartbeat before reporting success
set -u

REPO="$HOME/workspace/ron-telegram-bot"
LIVE="$HOME/workspace/telegram-bot"
STATE="$LIVE/ron_watchdog.json"

# 1. Sync code. config.py is intentionally excluded: the live runtime is
#    configured by ron_ai.json, and the repo's config.py is not used live.
for f in "$REPO"/*.py; do
    base="$(basename "$f")"
    [ "$base" = "config.py" ] && continue
    cp -p "$f" "$LIVE/$base"
done
cp -p "$REPO"/systemd/*.sh "$REPO"/systemd/*.py "$LIVE/systemd/" 2>/dev/null
for u in ron-watchdog.service ron-backup.service ron-backup.timer; do
    [ -f "$REPO/systemd/$u" ] && cp -p "$REPO/systemd/$u" "$LIVE/systemd/$u"
done
echo "code synced"

# 2. Reset crash counters so this planned restart is not counted as a crash.
python3 - "$STATE" <<'EOF'
import json, sys
p = sys.argv[1]
try:
    d = json.load(open(p))
except Exception:
    d = {}
d["consecutive_crashes"] = 0
d["last_restart_ts"] = 0
json.dump(d, open(p, "w"))
print("watchdog crash counters reset")
EOF

# 3. SIGTERM the daemon (isolated lookup: never match our own cmdline).
#    The lookup and the kill are separate steps per the pid-lookup footgun.
PIDS="$(pgrep -f 'ron_daemo[n]\.py' 2>/dev/null | grep -v "^$$$" || true)"
if [ -z "$PIDS" ]; then
    echo "no daemon running; watchdog will launch one on its next check"
else
    echo "$PIDS" | xargs -r kill 2>/dev/null
    echo "SIGTERM sent to daemon pid(s): $(echo $PIDS | tr '\n' ' ')"
fi

# 4. Wait for the watchdog's next check cycle to relaunch (up to ~100s),
#    then require a fresh heartbeat.
for i in $(seq 1 20); do
    sleep 5
    pid="$(pgrep -f 'ron_daemo[n]\.py' 2>/dev/null | head -1 || true)"
    if [ -n "$pid" ]; then
        hb_age="$(python3 -c "
import time, os
p = '$LIVE/ron_heartbeat.json'
try:
    print(int(time.time() - os.stat(p).st_mtime))
except OSError:
    print(99999)
")"
        if [ "$hb_age" -lt 60 ]; then
            echo "deployed: daemon pid=$pid heartbeat ${hb_age}s old"
            exit 0
        fi
    fi
done
echo "DEPLOY WARNING: no healthy daemon after ~100s — check ron_watchdog.log"
exit 1
