#!/usr/bin/env python3
"""Ron watchdog — supervisor for ron_daemon.py (Phase 3, 2026-09-20).

Two modes:
  --once : one check-and-repair pass, then exit (manual runs / cron-style)
  --loop : run forever, checking every CHECK_SECS (runs under systemd)

What it does on every pass:
  1. Validates ron_ai.json. If broken, restores the last-known-good copy
     (ron_ai.json.good) and quarantines the broken one.
  2. Checks the daemon: exactly one `python3 ron_daemon.py` process AND a
     fresh heartbeat file (the daemon writes ron_heartbeat.json every 30s).
     A live process with a stale heartbeat is hung -> killed and restarted.
     More than one daemon process -> keeps the oldest, kills the rest.
  3. Restarts a dead daemon with exponential backoff (60s, 5min, 15min,
     capped at 1h) so a crash-loop can't hot-spin the machine. Logs the
     crash reason (tail of ron_daemon.log) and the restart count.
  4. Snapshots ron_ai.json -> ron_ai.json.good whenever the daemon is
     healthy, so the fallback copy is always a config that actually ran.

State: ron_watchdog.json (restart counts, backoff bookkeeping).
Logs: stdout (journald under systemd) + ron_watchdog.log.
"""
import json
import os
import signal
import subprocess
import sys
import time

# Install dir: override with RON_BOT_DIR, otherwise the watchdog assumes
# it lives next to ron_daemon.py.
DIR = os.environ.get("RON_BOT_DIR") or os.path.dirname(os.path.abspath(__file__))
DAEMON = os.path.join(DIR, "ron_daemon.py")
DAEMON_LOG = os.path.join(DIR, "ron_daemon.log")
HEARTBEAT = os.path.join(DIR, "ron_heartbeat.json")
CONFIG = os.path.join(DIR, "ron_ai.json")
CONFIG_GOOD = os.path.join(DIR, "ron_ai.json.good")
STATE = os.path.join(DIR, "ron_watchdog.json")
WATCH_LOG = os.path.join(DIR, "ron_watchdog.log")

CHECK_SECS = 60            # loop-mode interval
HEARTBEAT_STALE_SECS = 150  # no heartbeat for this long -> hung
BACKOFF = [60, 300, 900, 3600]  # by consecutive_crash count (capped)
HEALTHY_RESET_SECS = 600   # healthy this long after a restart -> reset crash streak

_RUNNING = True


def _handle_signal(signum, frame):
    global _RUNNING
    _RUNNING = False


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def log(msg):
    line = f"[ron_watchdog] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(WATCH_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {"total_restarts": 0, "consecutive_crashes": 0,
                "last_restart_ts": 0.0, "last_healthy_ts": 0.0}


def save_state(st):
    try:
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, STATE)
    except Exception as e:
        log(f"state save failed: {e}")


def daemon_pids():
    """PIDs of running ron_daemon.py processes, oldest first.

    Matches the script path regardless of how python was invoked
    (bare `python3 ron_daemon.py` or the full
    `/usr/bin/python3 /path/to/ron_daemon.py`).
    """
    try:
        out = subprocess.run(
            ["pgrep", "-f", r"python[0-9.]* (.*/)?ron_daemon\.py$"],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    pids = []
    for line in out.split():
        try:
            pids.append(int(line))
        except ValueError:
            pass
    return sorted(pids)


def heartbeat_age():
    """Seconds since the daemon's last heartbeat, or None if unreadable."""
    try:
        with open(HEARTBEAT) as f:
            hb = json.load(f)
        return max(0.0, time.time() - float(hb.get("ts", 0)))
    except Exception:
        return None


def proc_age_secs(pid):
    """How long the process has been running, or None if unknown."""
    try:
        out = subprocess.run(["ps", "-o", "etimes=", "-p", str(pid)],
                             capture_output=True, text=True,
                             timeout=10).stdout.strip()
        return float(out)
    except Exception:
        return None


def check_config():
    """Validate ron_ai.json; fall back to last-known-good if broken."""
    try:
        with open(CONFIG) as f:
            json.load(f)
        return True
    except Exception as e:
        log(f"config {CONFIG} is broken ({e}); attempting fallback")
        try:
            with open(CONFIG_GOOD) as f:
                good = json.load(f)
        except Exception as e2:
            log(f"no usable last-known-good config ({e2}) — cannot start safely")
            return False
        # Quarantine the broken file, then restore the good one.
        try:
            q = CONFIG + f".broken.{int(time.time())}"
            os.replace(CONFIG, q)
            log(f"quarantined broken config -> {q}")
        except FileNotFoundError:
            pass
        except Exception as e3:
            log(f"quarantine failed: {e3}")
            return False
        try:
            with open(CONFIG, "w") as f:
                json.dump(good, f, indent=2)
            log("restored last-known-good config")
            return True
        except Exception as e4:
            log(f"config restore failed: {e4}")
            return False


def snapshot_good_config():
    """While the daemon is healthy, refresh the last-known-good copy."""
    try:
        with open(CONFIG) as f:
            cur = json.load(f)
        try:
            with open(CONFIG_GOOD) as f:
                old = json.load(f)
            if old == cur:
                return  # unchanged, skip the write
        except Exception:
            pass
        tmp = CONFIG_GOOD + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cur, f, indent=2)
        os.replace(tmp, CONFIG_GOOD)
    except Exception as e:
        log(f"good-config snapshot failed: {e}")


def crash_evidence():
    """Last lines of the daemon log — the likely crash reason."""
    try:
        out = subprocess.run(["tail", "-25", DAEMON_LOG],
                             capture_output=True, text=True,
                             timeout=10).stdout.strip()
        return out[-2000:] if out else "(log empty)"
    except Exception as e:
        return f"(could not read log: {e})"


def kill_pid(pid, name="daemon"):
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception as e:
        log(f"SIGTERM {pid} failed: {e}")
        return
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.5)
    try:
        os.kill(pid, signal.SIGKILL)
        log(f"SIGKILL {name} pid={pid} (did not exit on SIGTERM)")
    except ProcessLookupError:
        pass
    except Exception as e:
        log(f"SIGKILL {pid} failed: {e}")


def start_daemon():
    """Launch the daemon detached. Returns True if a process appears."""
    log("starting daemon")
    try:
        subprocess.Popen(
            [sys.executable, DAEMON],
            cwd=DIR, stdin=subprocess.DEVNULL,
            stdout=open(DAEMON_LOG, "a"), stderr=subprocess.STDOUT,
            start_new_session=True)
    except Exception as e:
        log(f"daemon launch failed: {e}")
        return False
    time.sleep(6)
    pids = daemon_pids()
    if pids:
        log(f"daemon up, pid={pids[0]}")
        return True
    log("daemon did not appear after launch")
    return False


def check_once():
    """One supervision pass. Returns True if the daemon is healthy now."""
    st = load_state()
    now = time.time()

    if not check_config():
        log("config unusable and no fallback — not starting the daemon")
        return False

    pids = daemon_pids()
    age = heartbeat_age()

    # More than one daemon -> keep the oldest, kill the rest.
    if len(pids) > 1:
        log(f"found {len(pids)} daemon processes {pids}; keeping oldest")
        for pid in pids[1:]:
            kill_pid(pid)
        pids = pids[:1]

    healthy = False
    if not pids:
        reason = "no daemon process"
    elif age is None:
        # No heartbeat yet — a fresh process gets a grace period for its
        # first heartbeat; an old one without heartbeats is broken.
        proc_age = proc_age_secs(pids[0])
        if proc_age is not None and proc_age < HEARTBEAT_STALE_SECS:
            log(f"daemon pid={pids[0]} started {proc_age:.0f}s ago, "
                f"waiting for first heartbeat")
            st["last_healthy_ts"] = now
            save_state(st)
            return True
        reason = (f"daemon pid={pids[0]} has no heartbeat file "
                  f"(age unknown or > {HEARTBEAT_STALE_SECS}s)")
        kill_pid(pids[0])
        pids = []
    elif age > HEARTBEAT_STALE_SECS:
        reason = (f"daemon pid={pids[0]} heartbeat stale "
                  f"({age:.0f}s > {HEARTBEAT_STALE_SECS}s) — hung")
        kill_pid(pids[0])
        pids = []
    else:
        healthy = True
        reason = ""

    if healthy:
        st["last_healthy_ts"] = now
        if (st["consecutive_crashes"]
                and now - st["last_restart_ts"] > HEALTHY_RESET_SECS):
            st["consecutive_crashes"] = 0
            log("daemon healthy 10+ min after restart — crash streak reset")
        save_state(st)
        snapshot_good_config()
        return True

    # --- repair path ---
    log(f"daemon unhealthy: {reason}")
    log(f"crash evidence (tail of ron_daemon.log):\n{crash_evidence()}")
    st["consecutive_crashes"] = st.get("consecutive_crashes", 0) + 1
    wait = BACKOFF[min(st["consecutive_crashes"] - 1, len(BACKOFF) - 1)]
    since_restart = now - st.get("last_restart_ts", 0)
    if since_restart < wait:
        log(f"backing off: crash #{st['consecutive_crashes']}, "
            f"next attempt in {wait - since_restart:.0f}s")
        save_state(st)
        return False
    if start_daemon():
        st["total_restarts"] = st.get("total_restarts", 0) + 1
        st["last_restart_ts"] = time.time()
        log(f"restart #{st['total_restarts']} done "
            f"(consecutive crashes: {st['consecutive_crashes']})")
    save_state(st)
    return False


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--once"
    log(f"watchdog {mode} (pid={os.getpid()})")
    if mode == "--loop":
        while _RUNNING:
            try:
                check_once()
            except Exception as e:
                log(f"check pass crashed: {e}")
            for _ in range(CHECK_SECS * 2):  # 0.5s granularity for shutdown
                if not _RUNNING:
                    break
                time.sleep(0.5)
        log("watchdog loop stopped")
    else:
        ok = check_once()
        log(f"one-shot pass: {'healthy' if ok else 'repaired/pending'}")


if __name__ == "__main__":
    main()
