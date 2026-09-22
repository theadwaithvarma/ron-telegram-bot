#!/usr/bin/env python3
"""Media-message trailers (v1.26.0): the AI brain emits trailers that the
daemon converts into native Telegram message types — no menu, no commands,
just conversational triggers.

Trailer syntax (documented in the system prompt):
  [dice]                                          -> sendDice
  [dice: 🎯]                                      -> sendDice with that emoji
  [poll: Question? | Option A | Option B]         -> sendPoll (regular)
  [pollm: Question? | A | B | C]                  -> sendPoll, multiple answers
  [quiz: Question? | opt1 | opt2 | opt3 | 2 | why] -> sendPoll type=quiz
      (correct index is 1-based; explanation optional)
  [location: 12.97, 77.59]                        -> sendLocation
  [livelocation: 12.97, 77.59]                    -> sendLocation, live 5 min
  [venue: 12.97, 77.59 | Title | Address]         -> sendVenue
  [contact: Display Name]                         -> sendContact, ALWAYS a
      clearly fake demo contact (Demo last name, +1-555 fictional number)
  [spoilerphoto: caption]                         -> sendPhoto has_spoiler=true,
      image generated locally with PIL (no external fetch)
  [mediagroup: caption]                           -> sendMediaGroup, 2 images

Parsing/validation is pure here; fire_actions() does the network calls via
rich_send.api_json / http_pool (same pattern as ron_imagen). Nothing in
this module imports ron_daemon, so there is no import cycle.

Skipped as already native (no trailer needed):
  rich text, code blocks, quotes, big quotes (>>> pull quote), details
  (<details> expandable), tables (markdown table -> native), checklists
  (- [ ] / - [x] -> native checkboxes) — all handled by ron_richblocks.
  Rich buttons, button styles, copy_text — the [buttons:]/[copy:]/...
  trailers already cover them. Message editing — streaming already edits
  messages live via editMessageText; no trailer needed.
"""
import io
import json
import os
import random
import re

import http_pool
import rich_send

__all__ = ["TRAILER_NAMES", "parse_media_trailers", "fire_actions",
           "demo_image_bytes"]

# Trailer names stripped from visible reply text by the daemon's
# strip_trailer_syntax (it reads this list).
TRAILER_NAMES = ("dice", "poll", "pollm", "quiz", "location",
                 "livelocation", "venue", "contact", "spoilerphoto",
                 "mediagroup")

_MAX_ACTIONS_PER_REPLY = 5  # sanity cap — the model is told to use sparingly

_DICE_EMOJI = {"🎲", "🎯", "🏀", "⚽", "🎳", "🎰"}

# Fictional-use number range (555-01XX): never a real person's number.
_FAKE_PHONE = "+15551234567"


def _find_bodies(reply, name):
    return re.findall(r"\[" + name + r":\s*([^\]\n]+?)\s*\]",
                      reply or "", re.I)


def _has_bare(reply, name):
    return bool(re.search(r"\[" + name + r"\]", reply or "", re.I))


def _valid_latlon(lat_s, lon_s):
    try:
        lat, lon = float(lat_s), float(lon_s)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _parse_poll(body, multi=False, quiz=False):
    kind = "quiz" if quiz else ("pollm" if multi else "poll")
    parts = [p.strip() for p in body.split("|")]
    parts = [p for p in parts if p]
    if len(parts) < 3:  # question + at least 2 options
        print(f"[media_trailers] dropped [{kind}:]: need "
              f"'Question? | opt1 | opt2', got {body!r}", flush=True)
        return None
    question = parts[0][:300]
    if quiz:
        # [quiz: Q | opt1 | opt2 | ... | correct_index | explanation?]
        # correct index is 1-based for the model; explanation optional.
        rest = parts[1:]
        explanation = ""
        correct_raw = None
        if len(rest) >= 3 and rest[-1] and not rest[-1].isdigit():
            # last part looks like an explanation, second-to-last the index
            if rest[-2].isdigit():
                explanation = rest[-1][:200]
                correct_raw = rest[-2]
                rest = rest[:-2]
            else:
                return None
        elif rest and rest[-1].isdigit():
            correct_raw = rest[-1]
            rest = rest[:-1]
        else:
            print(f"[media_trailers] dropped [quiz:]: need a 1-based "
                  f"correct_option index, got {body!r}", flush=True)
            return None
        options = [o[:100] for o in rest[:10]]
        if len(options) < 2:
            print(f"[media_trailers] dropped [quiz:]: need 2+ options, "
                  f"got {body!r}", flush=True)
            return None
        try:
            correct_idx = int(correct_raw) - 1
        except (TypeError, ValueError):
            print(f"[media_trailers] dropped [quiz:]: bad correct index "
                  f"{correct_raw!r}", flush=True)
            return None
        if not (0 <= correct_idx < len(options)):
            print(f"[media_trailers] dropped [quiz:]: correct index "
                  f"{correct_raw!r} out of range", flush=True)
            return None
        return {"kind": "quiz", "question": question, "options": options,
                "correct_option_id": correct_idx, "explanation": explanation}
    options = [o[:100] for o in parts[1:11]]
    if len(options) < 2:
        print(f"[media_trailers] dropped [{kind}:]: need 2+ options, "
              f"got {body!r}", flush=True)
        return None
    return {"kind": "pollm" if multi else "poll", "question": question,
            "options": options}


def parse_media_trailers(reply):
    """Return (actions, ) list of validated action dicts for the reply.

    Invalid trailers are dropped with a log line — never half-sent.
    """
    actions = []

    def add(a):
        if a and len(actions) < _MAX_ACTIONS_PER_REPLY:
            actions.append(a)

    for body in _find_bodies(reply, "dice"):
        emoji = body.strip()
        add({"kind": "dice",
             "emoji": emoji if emoji in _DICE_EMOJI else "🎲"})
    if _has_bare(reply, "dice"):
        add({"kind": "dice", "emoji": "🎲"})
    for body in _find_bodies(reply, "pollm"):
        add(_parse_poll(body, multi=True))
    for body in _find_bodies(reply, "poll"):
        # [pollm: ...] also matches the "poll" prefix pattern? No — the
        # regex requires "[poll:" literally, so pollm bodies are separate.
        add(_parse_poll(body))
    for body in _find_bodies(reply, "quiz"):
        add(_parse_poll(body, quiz=True))
    for body in _find_bodies(reply, "location"):
        coords = _valid_latlon(*([p.strip() for p in body.split(",")[:2]]
                                 + ["", ""])[:2])
        if coords:
            add({"kind": "location", "lat": coords[0], "lon": coords[1]})
        else:
            print(f"[media_trailers] dropped [location:]: bad coords "
                  f"{body!r}", flush=True)
    for body in _find_bodies(reply, "livelocation"):
        coords = _valid_latlon(*([p.strip() for p in body.split(",")[:2]]
                                 + ["", ""])[:2])
        if coords:
            add({"kind": "livelocation", "lat": coords[0], "lon": coords[1]})
        else:
            print(f"[media_trailers] dropped [livelocation:]: bad coords "
                  f"{body!r}", flush=True)
    for body in _find_bodies(reply, "venue"):
        parts = [p.strip() for p in body.split("|")]
        coords = _valid_latlon(*((parts[0].split(",") + ["", ""])[:2]))
        title = parts[1][:128] if len(parts) > 1 else ""
        address = parts[2][:64] if len(parts) > 2 else ""
        if coords and title and address:
            add({"kind": "venue", "lat": coords[0], "lon": coords[1],
                 "title": title, "address": address})
        else:
            print(f"[media_trailers] dropped [venue:]: need "
                  f"'lat, lon | Title | Address', got {body!r}", flush=True)
    for body in _find_bodies(reply, "contact"):
        name = re.sub(r"\s+", " ", body).strip()[:64] or "Demo"
        # ALWAYS fake: the number is fictional-use, the last name marks
        # the card as a demo. Never a real person's details.
        add({"kind": "contact", "first_name": name,
             "last_name": "Contact (demo)", "phone": _FAKE_PHONE})
    if _has_bare(reply, "contact"):
        add({"kind": "contact", "first_name": "Demo",
             "last_name": "Contact (demo)", "phone": _FAKE_PHONE})
    for body in _find_bodies(reply, "spoilerphoto"):
        add({"kind": "spoilerphoto", "caption": body.strip()[:1024]})
    if _has_bare(reply, "spoilerphoto"):
        add({"kind": "spoilerphoto", "caption": ""})
    for body in _find_bodies(reply, "mediagroup"):
        add({"kind": "mediagroup", "caption": body.strip()[:1024]})
    if _has_bare(reply, "mediagroup"):
        add({"kind": "mediagroup", "caption": ""})
    return actions


# ---------------------------------------------------------------------------
# Demo image generation: small harmless abstract images, PIL only, no fetch.
# ---------------------------------------------------------------------------

def demo_image_bytes(variant=0):
    """Return JPEG bytes of a small abstract demo image."""
    from PIL import Image, ImageDraw
    rng = random.Random(20260922 + variant)
    w, h = 640, 480
    img = Image.new("RGB", (w, h))
    px = img.load()
    c1 = (rng.randint(20, 60), rng.randint(40, 90), rng.randint(90, 150))
    c2 = (rng.randint(150, 230), rng.randint(120, 200), rng.randint(60, 130))
    for y in range(h):
        t = y / h
        for x in range(w):
            s = (x / w + y / h) / 2
            px[x, y] = tuple(int(c1[i] + (c2[i] - c1[i]) * (t * 0.6 + s * 0.4))
                             for i in range(3))
    d = ImageDraw.Draw(img)
    for _ in range(5):
        x0, y0 = rng.randint(0, w), rng.randint(0, h)
        r = rng.randint(40, 140)
        col = (rng.randint(200, 255), rng.randint(200, 255),
               rng.randint(200, 255), 90)
        d.ellipse([x0 - r, y0 - r, x0 + r, y0 + r], outline=col, width=6)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def _post_multipart(method, fields, files):
    """Multipart POST to the Bot API (pattern copied from ron_imagen)."""
    boundary = "----ronmedia" + os.urandom(8).hex()
    parts = []
    for name, value in fields:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; '
            f'name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, filename, data in files:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; '
            f'name="{name}"; filename="{filename}"\r\n'
            f'Content-Type: image/jpeg\r\n\r\n'.encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    try:
        status, raw = http_pool.pool_for("api.telegram.org").post(
            f"/bot{rich_send._SURROGATE}/{method}", body,
            f"multipart/form-data; boundary={boundary}", 60)
        if status != 200:
            return {"ok": False, "error":
                    f"HTTP {status}: {raw[:200].decode('utf-8', 'replace')}"}
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def _base_fields(chat_id, thread_id=None, business_connection_id=None):
    f = [("chat_id", str(chat_id))]
    if thread_id:
        f.append(("message_thread_id", str(thread_id)))
    if business_connection_id:
        f.append(("business_connection_id", str(business_connection_id)))
    return f


def _json_payload(chat_id, thread_id=None, business_connection_id=None,
                  **kw):
    p = {"chat_id": chat_id}
    if thread_id:
        p["message_thread_id"] = thread_id
    if business_connection_id:
        p["business_connection_id"] = business_connection_id
    p.update(kw)
    return p


def fire_actions(actions, chat_id, reply_to=None, thread_id=None,
                 business_connection_id=None, sent_msg_id=None):
    """Execute validated media actions. Returns number that landed ok.

    Never raises — a failed action is logged, the rest still fire.
    """
    ok_count = 0
    for a in actions or []:
        try:
            if _fire_one(a, chat_id, thread_id, business_connection_id,
                         sent_msg_id):
                ok_count += 1
        except Exception as e:
            print(f"[media_trailers] action {a.get('kind')} failed: {e}",
                  flush=True)
    return ok_count


def _fire_one(a, chat_id, thread_id, business_connection_id, sent_msg_id):
    kind = a.get("kind")
    base = dict(chat_id=chat_id)
    if thread_id:
        base["message_thread_id"] = thread_id
    if business_connection_id:
        base["business_connection_id"] = business_connection_id

    if kind == "dice":
        r = rich_send.api_json("sendDice", _json_payload(
            chat_id, thread_id, business_connection_id,
            emoji=a.get("emoji", "🎲")))
    elif kind in ("poll", "pollm"):
        p = _json_payload(chat_id, thread_id, business_connection_id,
                          question=a["question"], options=a["options"],
                          is_anonymous=True)
        if kind == "pollm":
            p["allows_multiple_answers"] = True
        r = rich_send.api_json("sendPoll", p)
    elif kind == "quiz":
        p = _json_payload(chat_id, thread_id, business_connection_id,
                          question=a["question"], options=a["options"],
                          is_anonymous=True, type="quiz",
                          correct_option_id=a["correct_option_id"])
        if a.get("explanation"):
            p["explanation"] = a["explanation"]
        r = rich_send.api_json("sendPoll", p)
    elif kind == "location":
        r = rich_send.api_json("sendLocation", _json_payload(
            chat_id, thread_id, business_connection_id,
            latitude=a["lat"], longitude=a["lon"]))
    elif kind == "livelocation":
        r = rich_send.api_json("sendLocation", _json_payload(
            chat_id, thread_id, business_connection_id,
            latitude=a["lat"], longitude=a["lon"], live_period=300))
    elif kind == "venue":
        r = rich_send.api_json("sendVenue", _json_payload(
            chat_id, thread_id, business_connection_id,
            latitude=a["lat"], longitude=a["lon"],
            title=a["title"], address=a["address"]))
    elif kind == "contact":
        r = rich_send.api_json("sendContact", _json_payload(
            chat_id, thread_id, business_connection_id,
            phone_number=a["phone"], first_name=a["first_name"],
            last_name=a["last_name"]))
    elif kind == "spoilerphoto":
        fields = _base_fields(chat_id, thread_id, business_connection_id)
        fields.append(("has_spoiler", "true"))
        if a.get("caption"):
            fields.append(("caption", a["caption"]))
        r = _post_multipart("sendPhoto", fields,
                            [("photo", "demo.jpg", demo_image_bytes(0))])
    elif kind == "mediagroup":
        media = [
            {"type": "photo", "media": "attach://p0"},
            {"type": "photo", "media": "attach://p1"},
        ]
        if a.get("caption"):
            media[0]["caption"] = a["caption"]
        fields = _base_fields(chat_id, thread_id, business_connection_id)
        fields.append(("media", json.dumps(media)))
        r = _post_multipart("sendMediaGroup", fields,
                            [("p0", "demo0.jpg", demo_image_bytes(1)),
                             ("p1", "demo1.jpg", demo_image_bytes(2))])
    else:
        print(f"[media_trailers] unknown action kind: {kind}", flush=True)
        return False
    if r.get("ok"):
        print(f"[media_trailers] {kind} ok (chat={chat_id})", flush=True)
        return True
    print(f"[media_trailers] {kind} failed: {str(r)[:150]}", flush=True)
    return False
