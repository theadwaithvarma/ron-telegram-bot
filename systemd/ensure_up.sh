#!/bin/bash
# ensure_up.sh — idempotent Ron bot health enforcer.
#
# Why this exists: the container's /etc is ephemeral, so every container
# restart wipes the systemd units and kills both the daemon and the
# watchdog. Nothing inside the box can run at boot (no cron, no user
# units, no persistent /etc). This script is run every 5 minutes by a
# Muse cron (which lives outside the container) and restores everything.
#
# Safe to run any time: when healthy it is a <3s no-op. It only sends the
# owner a Telegram message on state *transitions* (down->up, up->down),
# never on routine healthy checks.
set -u

DIR="$HOME/workspace/telegram-bot"
STATE_FILE="$DIR/ensure_state.json"
ALERT="$DIR/systemd/alert_owner.py"
HEARTBEAT_STALE_SECS=180

recovered=""

# 1. Systemd units present? (wiped on every container restart)
if [ ! -f /etc/systemd/system/ron-watchdog.service ]; then
    "$DIR/systemd/install.sh" >/dev/null 2>&1
    recovered="${recovered}units "
fi

# 2. Watchdog active? A restart SIGTERM counts as a "crash" in its
#    state file, so reset the backoff counters before (re)starting.
if [ "$(systemctl is-active ron-watchdog 2>/dev/null)" != "active" ]; then
    python3 - "$DIR" <<'EOF' 2>/dev/null
import json, sys
p = sys.argv[1] + "/ron_watchdog.json"
try:
    d = json.load(open(p))
    d["consecutive_crashes"] = 0
    d["last_restart_ts"] = 0
    json.dump(d, open(p, "w"))
except Exception:
    pass
EOF
    systemctl restart ron-watchdog 2>/dev/null
    recovered="${recovered}watchdog "
    sleep 5
fi

# 3. Daemon alive with a fresh heartbeat?
daemon_ok() {
    local pid hb now
    pid="$(pgrep -f 'ron_daemo[n]\.py' 2>/dev/null | head -1)"
    [ -n "$pid" ] || return 1
    hb="$(stat -c '%Y' "$DIR/ron_heartbeat.json" 2>/dev/null || echo 0)"
    now="$(date +%s)"
    [ $((now - hb)) -lt "$HEARTBEAT_STALE_SECS" ]
}

still_down=""
if ! daemon_ok; then
    sleep 75  # give the watchdog one check cycle to notice and relaunch
    if daemon_ok; then
        recovered="${recovered}daemon "
    else
        still_down=1
    fi
fi

# 4. OOM protection: the daemon previously ran with oom_score 670 —
#    first in line if the box ever faces memory pressure. Deprioritize
#    both the daemon and the watchdog (best effort, needs root).
for p in $(pgrep -f 'ron_daemo[n]\.py' 2>/dev/null); do
    echo -500 > "/proc/$p/oom_score_adj" 2>/dev/null || true
done
wpid="$(pgrep -f 'ron_watchdo[g]\.py' 2>/dev/null | head -1)"
[ -n "$wpid" ] && echo -500 > "/proc/$wpid/oom_score_adj" 2>/dev/null || true

# 5. Transition alerts only.
last="up"
if [ -f "$STATE_FILE" ]; then
    last="$(python3 -c "import json;print(json.load(open('$STATE_FILE')).get('state','up'))" 2>/dev/null || echo up)"
fi
if [ -n "$still_down" ]; then cur="down"; else cur="up"; fi
if [ "$cur" != "$last" ]; then
    if [ "$cur" = "up" ]; then
        python3 "$ALERT" "🔄 Ron went down (container restart) and I brought it back automatically (${recovered:-recovered}). Back online." >/dev/null 2>&1
    else
        python3 "$ALERT" "🚨 Ron is down and auto-recovery failed. Needs a human look — check the VM." >/dev/null 2>&1
    fi
    printf '{"state":"%s","ts":%s}\n' "$cur" "$(date +%s)" > "$STATE_FILE"
fi

[ "$cur" = "up" ] && echo "OK ${recovered:-healthy}" || echo "DOWN"
