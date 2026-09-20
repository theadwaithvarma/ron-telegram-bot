#!/usr/bin/env python3
"""Image reading for the Ron Telegram bot.

Pipeline: Telegram photo -> getFile -> download -> downscale (PIL) ->
base64 JPEG -> ron_ai.vision_stream (Gemini vision).

Privacy: image bytes never touch durable storage. The download goes to
a /tmp file that is deleted right after encoding; chat history stores
"[photo]", never pixels.

Rate limit (Adwaith, 2026-09-20): 5 images per rolling 6 hours per user,
owner exempt. Only timestamps are kept, in ron_vision_limits.json.
"""
import base64
import io
import json
import os
import tempfile
import threading
import time
import urllib.request

import rich_send

# Downscale target: at ~768px longest side a photo costs ~258 tokens per
# 768x768 tile on Gemini — a typical photo lands at 258-516 tokens, about
# a medium text message. Bigger buys almost nothing for chat answers.
MAX_SIDE = 768
JPEG_QUALITY = 80
MAX_BYTES = 15 * 1024 * 1024  # refuse absurd downloads

# 5 images per rolling 6 hours per user; owner is exempt.
LIMIT_N = 5
LIMIT_WINDOW_SECS = 6 * 3600
LIMIT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "ron_vision_limits.json")

_lock = threading.Lock()


def fetch_photo_b64(file_id):
    """Download a Telegram photo file_id, downscale, return base64 JPEG.

    Raises RuntimeError with a short reason on any failure (bad file_id,
    download error, undecodable image). The temp file is always removed.
    """
    r = rich_send.api_json("getFile", {"file_id": file_id})
    if not r.get("ok"):
        raise RuntimeError("telegram getFile failed")
    file_path = (r.get("result") or {}).get("file_path")
    if not file_path:
        raise RuntimeError("telegram getFile returned no path")
    # The hsurr: surrogate goes literally in the path — the egress proxy
    # swaps in the real token (see AGENTS.md connector lesson).
    url = f"https://api.telegram.org/file/bot{rich_send._SURROGATE}/{file_path}"
    tmp = tempfile.NamedTemporaryFile(suffix=".img", delete=False,
                                      dir="/tmp")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ron-bot/1"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise RuntimeError("image too large")
        tmp.write(data)
        tmp.close()
        return _downscale_b64(tmp.name)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _downscale_b64(path):
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        scale = min(1.0, MAX_SIDE / max(w, h))
        if scale < 1.0:
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                           Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=JPEG_QUALITY)
        return base64.b64encode(buf.getvalue()).decode("ascii")


def _load_limits():
    try:
        with open(LIMIT_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_limits(d):
    tmp = LIMIT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, LIMIT_FILE)


def check_limit(user_id, owner_id, now=None):
    """(allowed, retry_after_secs) for the 5-per-6h image rule.

    The owner is always allowed. Sliding window: timestamps older than
    6h are pruned on every check.
    """
    if user_id == owner_id:
        return True, 0.0
    now = time.time() if now is None else now
    with _lock:
        d = _load_limits()
        key = str(user_id)
        uses = [t for t in d.get(key, [])
                if isinstance(t, (int, float)) and now - t < LIMIT_WINDOW_SECS]
        d[key] = uses
        _save_limits(d)
    if len(uses) < LIMIT_N:
        return True, 0.0
    oldest = min(uses)
    return False, max(0.0, oldest + LIMIT_WINDOW_SECS - now)


def record_use(user_id, owner_id, now=None):
    """Log one image read. Owner reads aren't tracked (exempt)."""
    if user_id == owner_id:
        return
    now = time.time() if now is None else now
    with _lock:
        d = _load_limits()
        key = str(user_id)
        uses = [t for t in d.get(key, [])
                if isinstance(t, (int, float)) and now - t < LIMIT_WINDOW_SECS]
        uses.append(now)
        d[key] = uses
        _save_limits(d)


def stats():
    """Small usage summary for /health: images read in the last 24h."""
    now = time.time()
    with _lock:
        d = _load_limits()
    n = sum(1 for uses in d.values() for t in uses
            if isinstance(t, (int, float)) and now - t < 86400)
    return {"images_24h": n}
