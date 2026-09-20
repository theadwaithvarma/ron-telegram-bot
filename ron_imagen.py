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
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request

import rich_send

# Free, keyless image generation. A random seed per request so repeated
# prompts don't return the same cached picture.
# Model chain: flux first (full-quality diffusion, complete renders),
# turbo as fallback when flux is saturated (turbo is a faster distilled
# model; under load it sometimes returns cut-off images, which the
# integrity check below catches and retries). Both are free/keyless on
# the same endpoint.
_API_BASE = "https://image.pollinations.ai/prompt"
_MODELS = ("flux", "turbo")
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


def build_url(prompt, model=None):
    """Public so tests can assert the request shape without network.

    model defaults to the first entry of _MODELS (best quality first).
    """
    q = urllib.parse.quote(prompt[:MAX_PROMPT_CHARS], safe="")
    seed = random.randint(1, 999_999_999)
    model = model or _MODELS[0]
    return (f"{_API_BASE}/{q}?width={_WIDTH}&height={_HEIGHT}"
            f"&nologo=true&model={model}&seed={seed}")


class _TruncatedImage(Exception):
    """Partial download: the connection dropped or the stream was cut.

    Transient — safe to retry, and worth failing over to the next model.
    (Kept separate from RuntimeError so fetch_image doesn't treat it as
    a definitive provider answer.)"""


# Download backend: curl first. Python's urllib TLS fingerprint gets its
# connections killed mid-stream by the provider's CDN through this egress
# path (10/10 curl downloads complete, 0/10 urllib) — curl is unaffected.
# Falls back to urllib where curl isn't installed.
_CURL_BIN = shutil.which("curl")


def _validate_image(data, want=None):
    """Shared integrity gate for both download backends.

    want: declared Content-Length, or None if unknown (chunked).
    Raises RuntimeError for definitive failures (not an image / absurd
    size — not worth retrying) and _TruncatedImage for cut-off downloads
    (transient — retry / fail over).
    """
    if len(data) > MAX_BYTES:
        raise RuntimeError("image too large")
    if len(data) < 1024 or not data.startswith(b"\xff\xd8\xff"):
        # Pollinations returns JPEG; anything else is an error page —
        # definitive, not worth retrying.
        raise RuntimeError("provider returned no image")
    if want is not None and len(data) != want:
        # Connection died mid-download; the missing tail renders as grey
        # in most viewers.
        raise _TruncatedImage(f"short download ({len(data)}/{want} bytes)")
    if not data.rstrip().endswith(b"\xff\xd9"):
        # No JPEG end-of-image marker: the file was cut off mid-stream.
        raise _TruncatedImage("image data cut off mid-stream")
    return data


def _download_via_curl(url):
    hdr_fd, hdr_path = tempfile.mkstemp(dir="/tmp", prefix="ronimg_h")
    os.close(hdr_fd)
    body_fd, body_path = tempfile.mkstemp(dir="/tmp", prefix="ronimg_b")
    os.close(body_fd)
    try:
        p = subprocess.run(
            [_CURL_BIN, "-sS", "--max-time", str(_TIMEOUT),
             "-A", "ron-bot/1",
             "-D", hdr_path, "-o", body_path, url],
            capture_output=True, timeout=_TIMEOUT + 30)
        if p.returncode != 0:
            raise _TruncatedImage(
                "curl: " + p.stderr.decode("utf-8", "replace")[:120])
        with open(hdr_path, "rb") as f:
            hdr = f.read().decode("latin1", "replace")
        # One header block per hop (proxy CONNECT + origin); the last
        # status line and last Content-Length belong to the origin.
        codes = re.findall(r"^HTTP/\S+\s+(\d+)", hdr, re.M)
        if not codes or codes[-1] != "200":
            raise _TruncatedImage(
                f"provider http {codes[-1] if codes else '?'}")
        want = None
        for m in re.finditer(r"^Content-Length:\s*(\d+)\s*$", hdr,
                             re.M | re.I):
            want = int(m.group(1))
        with open(body_path, "rb") as f:
            data = f.read(MAX_BYTES + 1)
        return _validate_image(data, want)
    finally:
        for pth in (hdr_path, body_path):
            try:
                os.unlink(pth)
            except OSError:
                pass


def _download_via_urllib(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ron-bot/1"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        try:
            want = int(resp.headers.get("Content-Length"))
        except (TypeError, ValueError):
            want = None  # chunked encoding — the EOI check below decides
        if want is not None and want > MAX_BYTES:
            raise RuntimeError("image too large")
        # Read in a loop: a single read(n) on a socket can silently
        # return fewer than n bytes (short read, typically one 8KB
        # chunk) with no error. The old code trusted the first read,
        # which is exactly how half-grey images got uploaded.
        cap = min(want, MAX_BYTES) if want is not None else MAX_BYTES
        chunks, got = [], 0
        while got < cap:
            chunk = resp.read(min(65536, cap - got))
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
        data = b"".join(chunks)
        if got >= cap and resp.read(1):
            raise RuntimeError("image too large")
    return _validate_image(data, want)


def _download_once(url):
    """One download attempt: curl first, urllib fallback.

    Raises RuntimeError (definitive) or _TruncatedImage (transient).
    """
    if _CURL_BIN:
        try:
            return _download_via_curl(url)
        except RuntimeError:
            raise  # definitive — no point trying urllib
        except _TruncatedImage:
            pass  # fall through to the stdlib backend
    return _download_via_urllib(url)


def fetch_image(prompt):
    """Generate an image, save to a /tmp file, return the path.

    The caller must delete the file after uploading. Raises RuntimeError
    with a short reason on any failure (network, bad response, empty).
    Transient flakes (HTTP 429/5xx, dropped connections, timeouts,
    cut-off downloads) are retried a few times with backoff per model,
    then we fail over to the next model in _MODELS. Definitive answers
    (empty prompt, non-image response) fail fast with no retry.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise RuntimeError("empty prompt")
    last_err = "unknown"
    for model in _MODELS:
        url = build_url(prompt, model)
        for attempt in range(_FETCH_ATTEMPTS):
            try:
                data = _download_once(url)
            except RuntimeError:
                raise  # definitive — no retry, no failover
            except Exception as e:
                # Transient (network flake, timeout, _TruncatedImage):
                # back off, then retry; after _FETCH_ATTEMPTS, fail over
                # to the next model.
                last_err = f"{model}: {e}"
                if attempt < _FETCH_ATTEMPTS - 1:
                    time.sleep(_FETCH_BACKOFF_SECS[
                        min(attempt, len(_FETCH_BACKOFF_SECS) - 1)])
                continue
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
        # this model exhausted its attempts — try the next one
    raise RuntimeError(f"image service unreachable: {last_err}")


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
