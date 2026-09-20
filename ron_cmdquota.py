#!/usr/bin/env python3
"""Sliding-window per-user quotas for explicit commands (/factcheck, /think).

Same shape as ron_imagen/ron_vision limits, but parameterized so each
command declares its own budget (scope, N uses, window). Only timestamps
are stored, in ron_cmdquota_limits.json. Owner is always exempt.
"""
import json
import os
import threading
import time

_LIMIT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "ron_cmdquota_limits.json")

_lock = threading.Lock()


def _load_limits():
    try:
        with open(_LIMIT_FILE) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_limits(d):
    tmp = _LIMIT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, _LIMIT_FILE)


def _prune(uses, now, window_secs):
    return [t for t in uses
            if isinstance(t, (int, float)) and now - t < window_secs]


def check_limit(scope, user_id, owner_id, n, window_secs, now=None):
    """(allowed, retry_after_secs) for a command's budget.

    The owner is always allowed. Sliding window: timestamps older than
    the window are pruned on every check.
    """
    if user_id == owner_id:
        return True, 0.0
    now = time.time() if now is None else now
    with _lock:
        d = _load_limits()
        uses = _prune(d.get(str(scope), {}).get(str(user_id), []),
                      now, window_secs)
        entry = d.setdefault(str(scope), {})
        entry[str(user_id)] = uses
        _save_limits(d)
    if len(uses) < n:
        return True, 0.0
    oldest = min(uses)
    return False, max(0.0, oldest + window_secs - now)


def record_use(scope, user_id, owner_id, n, window_secs, now=None):
    """Log one command use. Owner uses aren't tracked (exempt)."""
    if user_id == owner_id:
        return
    now = time.time() if now is None else now
    with _lock:
        d = _load_limits()
        entry = d.setdefault(str(scope), {})
        uses = _prune(entry.get(str(user_id), []), now, window_secs)
        uses.append(now)
        entry[str(user_id)] = uses
        _save_limits(d)


def usage(scope, user_id, owner_id, n, window_secs, now=None):
    """Current in-window use count (owner reports 0 — exempt)."""
    if user_id == owner_id:
        return 0
    now = time.time() if now is None else now
    with _lock:
        d = _load_limits()
        uses = _prune(d.get(str(scope), {}).get(str(user_id), []),
                      now, window_secs)
        return len(uses)
