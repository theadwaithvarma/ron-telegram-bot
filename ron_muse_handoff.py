"""ron_muse_handoff — the /muse owner bridge between the Telegram bot and Muse.

Design (v1.32.0):
- /muse is owner-only and handled entirely by ron_daemon, which never calls
  a provider for it: it atomically queues the request under the handoff dir
  and acks immediately (measured normal-bot path untouched).
- A lightweight Muse-side hook polls every ~30s, wakes only when a request
  file exists, does the work as Muse (full tools/files/context), and writes
  a response file back.
- A daemon sweeper thread drains response files and send()s them back to
  the chat that issued the request (reply_to the /muse command message).

Zero model tokens are consumed while the queue is idle — the poller is a
plain shell script. Idempotent: claim/complete markers make restarts and
double-wakes safe. No secrets may ever be written into request/response
payloads.

Queue layout under <base>/:
  requests/<req_id>.json      — submitted request (daemon writes)
  requests/<req_id>.claimed   — worker started (hook worker writes)
  requests/<req_id>.done      — response delivered/written (worker writes)
  responses/<req_id>.json     — completed answer (worker writes, daemon drains)
"""

import glob
import json
import os
import time
import uuid

BASE_DIR = None  # set via set_dir(); defaults to the live bot dir


def set_dir(d):
    global BASE_DIR
    BASE_DIR = d


def _base():
    if BASE_DIR:
        return BASE_DIR
    return os.path.expanduser("~/workspace/telegram-bot")


def _dirs():
    base = os.path.join(_base(), "handoff")
    req = os.path.join(base, "requests")
    res = os.path.join(base, "responses")
    os.makedirs(req, mode=0o700, exist_ok=True)
    os.makedirs(res, mode=0o700, exist_ok=True)
    # makedirs won't fix perms on pre-existing dirs — enforce owner-only.
    for d in (os.path.join(_base(), "handoff"), req, res):
        try:
            os.chmod(d, 0o700)
        except Exception:
            pass
    return req, res


def _atomic_write(path, payload):
    """Write JSON atomically: tmp file + fsync + rename, mode 0600."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def submit_request(chat_id, message_id, user_id, user_name, command_text,
                   reply_msg=None):
    """Queue an owner /muse request. Returns the request id.

    reply_msg: the replied-to Telegram message dict (or None) — its
    text/caption, sender and date are captured so Muse gets the context.
    """
    req_dir, _ = _dirs()
    req_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    payload = {
        "id": req_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "user_id": user_id,
        "user_name": user_name or "",
        "command_text": command_text or "",
        "created_ts": int(time.time()),
    }
    if reply_msg:
        rm = reply_msg if isinstance(reply_msg, dict) else {}
        sender = rm.get("from") or {}
        payload["reply_context"] = {
            "message_id": rm.get("message_id"),
            "from_id": sender.get("id"),
            "from_name": (sender.get("first_name", "")
                          + " " + sender.get("last_name", "")).strip()
            or sender.get("username", ""),
            "from_username": sender.get("username", ""),
            "date": rm.get("date"),
            "text": rm.get("text") or rm.get("caption") or "",
            "has_photo": bool(rm.get("photo")),
            "has_document": bool(rm.get("document")),
        }
    _atomic_write(os.path.join(req_dir, req_id + ".json"), payload)
    return req_id


def pending_requests():
    """Request dicts that have no .claimed and no .done marker, oldest first."""
    req_dir, _ = _dirs()
    out = []
    for path in sorted(glob.glob(os.path.join(req_dir, "*.json"))):
        base = path[:-5]
        if os.path.exists(base + ".claimed") or os.path.exists(base + ".done"):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                out.append(json.load(f))
        except Exception:
            continue
    return out


def claim_request(req_id):
    """Mark a request as being worked on. Idempotent."""
    req_dir, _ = _dirs()
    marker = os.path.join(req_dir, req_id + ".claimed")
    try:
        with open(marker, "a"):
            os.utime(marker, None)
        os.chmod(marker, 0o600)
    except Exception:
        pass


def write_response(req_id, reply_text, deliver_to_app=False,
                   extra=None):
    """Publish the worker's answer. Idempotent; then marks request done."""
    _, res_dir = _dirs()
    req_dir = os.path.join(_base(), "handoff", "requests")
    payload = {
        "id": req_id,
        "reply_text": reply_text or "",
        "deliver_to_app": bool(deliver_to_app),
        "created_ts": int(time.time()),
    }
    if extra:
        payload["extra"] = extra
    _atomic_write(os.path.join(res_dir, req_id + ".json"), payload)
    try:
        with open(os.path.join(req_dir, req_id + ".done"), "a"):
            pass
    except Exception:
        pass


def peek_responses():
    """Read all pending response files WITHOUT removing them. Returns
    [(req, response)]. The caller must ack_response(req_id) only after
    the answer was actually delivered — a failed send keeps the file so
    a later sweep retries instead of losing the answer."""
    _, res_dir = _dirs()
    req_dir = os.path.join(_base(), "handoff", "requests")
    out = []
    for path in sorted(glob.glob(os.path.join(res_dir, "*.json"))):
        req_id = os.path.basename(path)[:-5]
        try:
            with open(path, encoding="utf-8") as f:
                resp = json.load(f)
        except Exception:
            continue
        req = None
        req_path = os.path.join(req_dir, req_id + ".json")
        try:
            with open(req_path, encoding="utf-8") as f:
                req = json.load(f)
        except Exception:
            pass
        out.append((req, resp))
    return out


def ack_response(req_id):
    """Remove a response file after successful delivery. Idempotent."""
    _, res_dir = _dirs()
    try:
        os.unlink(os.path.join(res_dir, req_id + ".json"))
    except Exception:
        pass


def drain_responses():
    """Read+remove all pending response files. Returns [(req, response)].

    Kept for compatibility; the daemon's sweeper uses peek/ack so a
    failed send never loses an answer.
    """
    out = peek_responses()
    for req, resp in out:
        ack_response(resp.get("id", ""))
    return out
