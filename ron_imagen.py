#!/usr/bin/env python3
"""Image generation for the Ron Telegram bot (/imagine).

Pipeline: prompt -> Pollinations image API (free, no key) -> JPEG bytes
-> /tmp temp file -> sendPhoto multipart upload -> temp file deleted.

Nothing runs locally: generation happens on the provider's GPUs, so this
adds zero weight to the daemon and never slows regular chat — it only
fires on an explicit /imagine, in a worker thread.

Privacy: image bytes never touch durable storage. The download goes to
a /tmp file that is deleted right after the upload; chat history stores
"[generated image]" plus the prompt — never pixels.

Rate limit (Adwaith, 2026-09-20): 10 images per rolling 6 hours per user,
owner exempt. Only timestamps are kept, in ron_imagen_limits.json.
"""
import json
import os
import random
import tempfile
import threading
import time
import urllib.parse
import urllib.request

import rich_send

# Free, keyless image generation. A random seed per request so repeated
# prompts don't return the same cached picture.
# model=turbo: the default backend 429s under load (their upstream Sana
# queue saturates); turbo is a faster distilled model that stays up.
_API_BASE = "https://image.pollinations.ai/prompt"
_MODEL = "turbo"
_WIDTH = 1024
_HEIGHT = 1024
_TIMEOUT = 180  # generation can take a while on the free tier
MAX_BYTES = 15 * 1024 * 1024  # refuse absurd downloads
MAX_PROMPT_CHARS = 400

# Transient network/provider flakes get a few retries with backoff;
# definitive answers (empty prompt, non-image response) fail fast.
_FETCH_ATTEMPTS = 3
_FETCH_BACKOFF_SECS = (2, 5)

# 10 images per rolling 6 hours per user; owner is exempt.
LIMIT_N = 10
LIMIT_WINDOW_SECS = 6 * 3600
LIMIT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "ron_imagen_limits.json")

_lock = threading.Lock()


def build_url(prompt):
    """Public so tests can assert the request shape without network."""
    q = urllib.parse.quote(prompt[:MAX_PROMPT_CHARS], safe="")
    seed = random.randint(1, 999_999_999)
    return (f"{_API_BASE}/{q}?width={_WIDTH}&height={_HEIGHT}"
            f"&nologo=true&model={_MODEL}&seed={seed}")


def _download_once(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ron-bot/1"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise RuntimeError("image too large")
    if len(data) < 1024 or not data.startswith(b"\xff\xd8\xff"):
        # Pollinations returns JPEG; anything else is an error page —
        # definitive, not worth retrying.
        raise RuntimeError("provider returned no image")
    return data


def fetch_image(prompt):
    """Generate an image, save to a /tmp file, return the path.

    The caller must delete the file after uploading. Raises RuntimeError
    with a short reason on any failure (network, bad response, empty).
    Transient flakes (HTTP 429/5xx, dropped connections, timeouts) are
    retried a few times with backoff; definitive answers fail fast.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise RuntimeError("empty prompt")
    url = build_url(prompt)
    for attempt in range(_FETCH_ATTEMPTS):
        try:
            data = _download_once(url)
            break
        except RuntimeError:
            raise  # definitive (empty/too large/not an image) — no retry
        except Exception as e:
            if attempt == _FETCH_ATTEMPTS - 1:
                raise RuntimeError(f"image service unreachable: {e}") from e
            time.sleep(_FETCH_BACKOFF_SECS[
                min(attempt, len(_FETCH_BACKOFF_SECS) - 1)])
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False,
                                      dir="/tmp")
    try:
        tmp.write(data)
        tmp.close()
        return tmp.name
    except Exception:
        try:
            tmp.close()
        except OSError:
            pass
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def send_photo(chat_id, path, caption="", reply_to=None, thread_id=None):
    """Upload a local image file via sendPhoto (multipart).

    Returns the parsed Bot API response. Image bytes go straight from
    /tmp to Telegram — never into history, memory, or logs.
    """
    boundary = "----ronimg" + os.urandom(8).hex()
    with open(path, "rb") as f:
        img = f.read()
    parts = []

    def field(name, value):
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; '
            f'name="{name}"\r\n\r\n{value}\r\n'.encode())

    field("chat_id", str(chat_id))
    if caption:
        field("caption", caption[:1024])  # Telegram photo-caption cap
        field("parse_mode", "Markdown")
    if reply_to:
        field("reply_parameters",
              json.dumps({"message_id": reply_to}))
    if thread_id:
        field("message_thread_id", str(thread_id))
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; '
        f'filename="image.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'
        .encode() + img + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    url = f"https://api.telegram.org/bot{rich_send._SURROGATE}/sendPhoto"
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "User-Agent": "ron-bot/1"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read(65536)
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:
        body_txt = ""
        try:
            body_txt = e.read().decode()[:300]  # noqa
        except Exception:
            pass
        return {"ok": False, "error": f"{e} {body_txt}"[:300]}


def delete_message(chat_id, message_id):
    """Best-effort single message delete (used to remove the ack)."""
    try:
        return rich_send.api_json("deleteMessage", {
            "chat_id": chat_id, "message_id": message_id})
    except Exception:
        return {"ok": False}


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
    """(allowed, retry_after_secs) for the 10-per-6h generation rule.

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
    """Log one generation. Owner generations aren't tracked (exempt)."""
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
    """Small usage summary for /health: images generated in 24h."""
    now = time.time()
    with _lock:
        d = _load_limits()
    n = sum(1 for uses in d.values() for t in uses
            if isinstance(t, (int, float)) and now - t < 86400)
    return {"images_gen_24h": n}
