#!/usr/bin/env python3
"""Offline tests for image reading (no network).

Run: python3 tests/test_vision.py  (or python3 -m unittest discover tests)

Covers:
- ron_vision rate limit: 5 images / rolling 6h, owner exempt, per-user
  isolation, window expiry, check-before-download ordering (failed
  fetches never consume quota).
- ron_vision image prep: downscale to <=768px, JPEG base64, temp cleanup.
- ron_ai.vision_stream: Gemini-only routing (never Groq), message shape
  with image_url part, RateLimitedError passthrough.
- ron_daemon photo hook: DM direct, group mention/reply gating,
  maintenance silence, limit notice without any download/API burn.
"""
import base64
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_ai
import ron_daemon
import ron_vision

OWNER = ron_daemon.OWNER_ID
USER_A = 111111
USER_B = 222222


def _mk_limits_file(test):
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    test.addCleanup(os.unlink, tmp.name)
    p = mock.patch.object(ron_vision, "LIMIT_FILE", tmp.name)
    p.start()
    test.addCleanup(p.stop)
    return tmp.name


def _test_jpeg(path, size=(2000, 1000), color=(200, 30, 30)):
    from PIL import Image
    im = Image.new("RGB", size, color)
    im.save(path, "JPEG", quality=90)


class FakeSSE:
    """Minimal stand-in for the urllib response _drain_stream reads."""
    def __init__(self, chunks):
        self._lines = list(chunks)
        self.closed = False

    def readline(self):
        if self._lines:
            return self._lines.pop(0)
        return b""

    def close(self):
        self.closed = True


def _sse_text(t):
    return ('data: {"choices":[{"delta":{"content":%s}}]}\n'
            % json.dumps(t)).encode()


class TestVisionLimits(unittest.TestCase):
    def setUp(self):
        _mk_limits_file(self)

    def test_owner_exempt(self):
        for _ in range(20):
            ron_vision.record_use(OWNER, OWNER)
        allowed, retry = ron_vision.check_limit(OWNER, OWNER)
        self.assertTrue(allowed)
        self.assertEqual(retry, 0.0)

    def test_five_allowed_sixth_blocked(self):
        for _ in range(5):
            allowed, _ = ron_vision.check_limit(USER_A, OWNER)
            self.assertTrue(allowed)
            ron_vision.record_use(USER_A, OWNER)
        allowed, retry = ron_vision.check_limit(USER_A, OWNER)
        self.assertFalse(allowed)
        self.assertGreater(retry, 0.0)
        self.assertLessEqual(retry, 6 * 3600)

    def test_check_does_not_consume(self):
        # Quota is only spent by record_use (called after a successful
        # fetch) — probing the limit never burns one of the 5.
        for _ in range(10):
            allowed, _ = ron_vision.check_limit(USER_A, OWNER)
            self.assertTrue(allowed)

    def test_window_expiry(self):
        old = 7 * 3600  # older than the 6h window
        with mock.patch("time.time", return_value=1_000_000.0):
            for _ in range(5):
                ron_vision.record_use(USER_A, OWNER)
        with mock.patch("time.time",
                        return_value=1_000_000.0 + old):
            allowed, _ = ron_vision.check_limit(USER_A, OWNER)
            self.assertTrue(allowed)

    def test_per_user_isolation(self):
        for _ in range(5):
            ron_vision.record_use(USER_A, OWNER)
        allowed, _ = ron_vision.check_limit(USER_B, OWNER)
        self.assertTrue(allowed)
        allowed, _ = ron_vision.check_limit(USER_A, OWNER)
        self.assertFalse(allowed)

    def test_owner_reads_not_tracked(self):
        ron_vision.record_use(OWNER, OWNER)
        with open(ron_vision.LIMIT_FILE) as f:
            raw = f.read().strip()
        d = json.loads(raw) if raw else {}
        self.assertNotIn(str(OWNER), d)

    def test_stats_counts_24h(self):
        ron_vision.record_use(USER_A, OWNER)
        ron_vision.record_use(USER_B, OWNER)
        st = ron_vision.stats()
        self.assertEqual(st["images_24h"], 2)

    def test_usage_counts_window(self):
        for _ in range(3):
            ron_vision.record_use(USER_A, OWNER)
        self.assertEqual(ron_vision.usage(USER_A, OWNER), 3)
        self.assertEqual(ron_vision.usage(USER_B, OWNER), 0)
        self.assertEqual(ron_vision.usage(OWNER, OWNER), 0)


class TestVisionImagePrep(unittest.TestCase):
    def test_downscale_bounds_longest_side(self):
        src = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        src.close()
        self.addCleanup(os.unlink, src.name)
        _test_jpeg(src.name, size=(2000, 1000))
        b64 = ron_vision._downscale_b64(src.name)
        raw = base64.b64decode(b64)
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        self.assertEqual(im.format, "JPEG")
        self.assertLessEqual(max(im.size), ron_vision.MAX_SIDE)
        # aspect preserved: 2:1 stays 2:1
        self.assertAlmostEqual(im.size[0] / im.size[1], 2.0, places=1)

    def test_small_image_not_upscaled(self):
        src = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        src.close()
        self.addCleanup(os.unlink, src.name)
        _test_jpeg(src.name, size=(400, 300))
        b64 = ron_vision._downscale_b64(src.name)
        from PIL import Image
        im = Image.open(io.BytesIO(base64.b64decode(b64)))
        self.assertEqual(im.size, (400, 300))

    def test_fetch_photo_b64_happy_path(self):
        src = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        src.close()
        self.addCleanup(os.unlink, src.name)
        _test_jpeg(src.name, size=(1600, 1200))
        with open(src.name, "rb") as f:
            img_bytes = f.read()
        before = set(os.listdir("/tmp"))
        with mock.patch.object(
                ron_vision.rich_send, "api_json",
                return_value={"ok": True,
                              "result": {"file_path": "photos/x.jpg"}}), \
             mock.patch("urllib.request.urlopen") as urlopen:
            fake_resp = mock.MagicMock()
            fake_resp.read.return_value = img_bytes
            urlopen.return_value.__enter__.return_value = fake_resp
            b64 = ron_vision.fetch_photo_b64("file-id-123")
        from PIL import Image
        im = Image.open(io.BytesIO(base64.b64decode(b64)))
        self.assertLessEqual(max(im.size), ron_vision.MAX_SIDE)
        # temp download removed
        self.assertEqual(set(os.listdir("/tmp")), before)

    def test_fetch_photo_b64_getfile_failure(self):
        with mock.patch.object(
                ron_vision.rich_send, "api_json",
                return_value={"ok": False, "error": "bad"}):
            with self.assertRaises(RuntimeError):
                ron_vision.fetch_photo_b64("nope")

    def test_fetch_photo_b64_bad_image(self):
        with mock.patch.object(
                ron_vision.rich_send, "api_json",
                return_value={"ok": True,
                              "result": {"file_path": "photos/x.jpg"}}), \
             mock.patch("urllib.request.urlopen") as urlopen:
            fake_resp = mock.MagicMock()
            fake_resp.read.return_value = b"this is not an image"
            urlopen.return_value.__enter__.return_value = fake_resp
            with self.assertRaises(Exception):
                ron_vision.fetch_photo_b64("file-id-123")


class TestVisionStream(unittest.TestCase):
    def test_gemini_only_routing_and_shape(self):
        seen = {}

        def fake_request(cfg, messages, stream=True, timeout=120,
                         cheap=False):
            seen["cfg"] = cfg
            seen["messages"] = messages
            return FakeSSE([_sse_text("a cat "), _sse_text("on a mat"),
                            b"data: [DONE]\n"])

        with mock.patch.object(ron_ai, "_request", fake_request):
            reply = ron_ai.vision_stream("QUJD", "what is this?")
        self.assertEqual(reply, "a cat on a mat")
        providers = seen["cfg"]["providers"]
        names = [p["name"] for p in providers]
        self.assertNotIn("groq", names)  # Groq key is text-only gpt-oss
        self.assertIn("gemini", names)
        models = [m for p in providers for m in p["models"]]
        self.assertIn("gemini-flash-lite-latest", models)  # lite first
        user_msg = seen["messages"][1]
        parts = user_msg["content"]
        kinds = [p["type"] for p in parts]
        self.assertEqual(kinds, ["text", "image_url"])
        self.assertEqual(parts[0]["text"], "what is this?")
        self.assertTrue(parts[1]["image_url"]["url"].startswith(
            "data:image/jpeg;base64,QUJD"))

    def test_default_question_when_no_caption(self):
        seen = {}

        def fake_request(cfg, messages, stream=True, timeout=120,
                         cheap=False):
            seen["messages"] = messages
            return FakeSSE([_sse_text("a sunset"), b"data: [DONE]\n"])

        with mock.patch.object(ron_ai, "_request", fake_request):
            ron_ai.vision_stream("QUJD", "")
        self.assertEqual(seen["messages"][1]["content"][0]["text"],
                         "Describe this image.")

    def test_rate_limited_propagates(self):
        def fake_request(cfg, messages, stream=True, timeout=120,
                         cheap=False):
            raise ron_ai.RateLimitedError("rate limited by all AI providers")

        with mock.patch.object(ron_ai, "_request", fake_request):
            with self.assertRaises(ron_ai.RateLimitedError):
                ron_ai.vision_stream("QUJD", "hi")

    def test_empty_stream_raises(self):
        def fake_request(cfg, messages, stream=True, timeout=120,
                         cheap=False):
            return FakeSSE([b"data: [DONE]\n"])

        with mock.patch.object(ron_ai, "_request", fake_request):
            with self.assertRaises(RuntimeError):
                ron_ai.vision_stream("QUJD", "hi")


def _photo_msg(chat_type="private", caption="", entities=None,
               reply_from_bot=False, user_id=USER_A):
    msg = {
        "message_id": 42,
        "from": {"id": user_id, "first_name": "Testy"},
        "chat": {"id": 777, "type": chat_type, "title": "Test Group"},
        "photo": [{"file_id": "small"}, {"file_id": "big"}],
        "caption": caption,
    }
    if entities:
        msg["caption_entities"] = entities
    if reply_from_bot:
        msg["reply_to_message"] = {"from": {"is_bot": True}}
    return msg


class TestPhotoHook(unittest.TestCase):
    def setUp(self):
        _mk_limits_file(self)
        self.send = mock.patch.object(ron_daemon, "send").start()
        self.vision_reply = mock.patch.object(
            ron_daemon, "ai_vision_reply").start()
        self.addCleanup(mock.patch.stopall)

    def _handle(self, msg):
        chat = msg["chat"]
        ron_daemon._handle_photo(msg, chat, msg["from"],
                                 msg["from"]["id"],
                                 msg["from"]["first_name"],
                                 chat["type"] == "private")

    def test_dm_photo_reaches_vision(self):
        # _handle_photo only gates; the download + vision run in the
        # worker thread. Largest Telegram size ("big") is passed.
        self._handle(_photo_msg(caption="what's this?"))
        self.vision_reply.assert_called_once_with(
            777, USER_A, "Testy", "what's this?", "big",
            private=True, reply_to=42, thread_id=None)

    def test_group_photo_ignored_without_trigger(self):
        self._handle(_photo_msg(chat_type="supergroup", caption="look"))
        self.vision_reply.assert_not_called()
        self.send.assert_not_called()

    def test_group_photo_with_caption_mention(self):
        cap = "hey @ronrebootedbot what is this"
        ent = [{"type": "mention", "offset": 4,
                "length": len("@ronrebootedbot")}]
        self._handle(_photo_msg(chat_type="supergroup", caption=cap,
                                entities=ent))
        self.vision_reply.assert_called_once()
        args, kw = self.vision_reply.call_args
        self.assertEqual(args[4], "big")
        self.assertFalse(kw["private"])
        self.assertEqual(kw["reply_to"], 42)

    def test_group_photo_reply_to_bot(self):
        self._handle(_photo_msg(chat_type="supergroup",
                                reply_from_bot=True))
        self.vision_reply.assert_called_once()

    def test_limit_blocks_before_download(self):
        for _ in range(5):
            ron_vision.record_use(USER_A, OWNER)
        self._handle(_photo_msg(caption="one more"))
        self.vision_reply.assert_not_called()  # no worker, no API burn
        user_notices = [c for c in self.send.call_args_list
                        if c[0][0] != OWNER]
        self.assertEqual(len(user_notices), 1)
        sent_text = user_notices[0][0][1]
        self.assertIn("image-reading limit", sent_text)
        self.assertNotIn("groq", sent_text.lower())
        self.assertNotIn("gemini", sent_text.lower())

    def test_owner_ignores_limit(self):
        for _ in range(9):
            ron_vision.record_use(USER_A, OWNER)
        self._handle(_photo_msg(user_id=OWNER))
        self.vision_reply.assert_called_once()

    def test_maintenance_silences_non_owner(self):
        with mock.patch.object(ron_daemon, "maintenance_on",
                               return_value=True):
            self._handle(_photo_msg(user_id=USER_A))
        self.vision_reply.assert_not_called()
        self.send.assert_not_called()

    def test_maintenance_allows_owner(self):
        with mock.patch.object(ron_daemon, "maintenance_on",
                               return_value=True):
            self._handle(_photo_msg(user_id=OWNER))
        self.vision_reply.assert_called_once()

    def test_vision_module_missing(self):
        with mock.patch.object(ron_daemon, "ron_vision", None):
            self._handle(_photo_msg())
        self.vision_reply.assert_not_called()
        self.send.assert_called_once()

    def test_limit_text_formatting(self):
        self.assertIn("~5h 0m", ron_daemon._vision_limit_text(5 * 3600 + 30))
        self.assertIn("~3m", ron_daemon._vision_limit_text(150))

    def test_handle_update_routes_photo(self):
        msg = _photo_msg()
        upd = {"update_id": 1, "message": msg}
        with mock.patch.object(ron_daemon, "_handle_photo") as hp:
            ron_daemon.handle_update(upd)
        hp.assert_called_once()


class SyncThread:
    """threading.Thread stand-in that runs the target inline, so worker
    logic can be tested deterministically without sleeps."""
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args or ()
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)

    def join(self, timeout=None):
        pass  # ran inline in start()

    def is_alive(self):
        return False


class TestVisionWorker(unittest.TestCase):
    """ai_vision_reply's worker: fetch -> quota -> vision -> send."""
    def setUp(self):
        _mk_limits_file(self)
        self.thread = mock.patch("threading.Thread", SyncThread).start()
        self.rich = mock.patch.object(ron_daemon, "send_rich_with_kb").start()
        self.stream = mock.patch.object(
            ron_daemon, "stream_vision_reply").start()
        self.addCleanup(mock.patch.stopall)

    def _call(self, **kw):
        args = dict(chat_id=777, user_id=USER_A, user_name="Testy",
                    caption="what's this?", file_id="big", private=True)
        args.update(kw)
        ron_daemon.ai_vision_reply(**args)

    def test_fetch_failure_never_consumes_quota(self):
        with mock.patch.object(ron_vision, "fetch_photo_b64",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(ron_vision, "record_use") as rec, \
             mock.patch.object(ron_ai, "vision_stream") as vs:
            self._call()
        rec.assert_not_called()
        vs.assert_not_called()
        self.stream.assert_not_called()
        self.rich.assert_called_once()
        self.assertIn("Couldn't grab", self.rich.call_args[0][1])

    def test_private_success_streams_and_records(self):
        with mock.patch.object(ron_vision, "fetch_photo_b64",
                               return_value="QUJD"), \
             mock.patch.object(ron_vision, "record_use") as rec:
            self._call()
        rec.assert_called_once_with(USER_A, OWNER)
        self.stream.assert_called_once_with(777, USER_A, "Testy",
                                            "what's this?", "QUJD")

    def test_group_success_single_send(self):
        with mock.patch.object(ron_vision, "fetch_photo_b64",
                               return_value="QUJD"), \
             mock.patch.object(ron_ai, "vision_stream",
                               return_value="a cat"), \
             mock.patch.object(ron_ai, "_save_turn") as save:
            self._call(private=False, reply_to=42)
        self.stream.assert_not_called()
        self.rich.assert_called_once()
        args, _ = self.rich.call_args
        self.assertEqual(args[1], "a cat")
        self.assertEqual(args[2], 42)  # reply_to threading preserved
        save.assert_called_once()
        # history stores a marker, never image bytes
        self.assertNotIn("QUJD", save.call_args[0][1])
        self.assertIn("[photo]", save.call_args[0][1])

    def test_vision_rate_limit_is_provider_neutral(self):
        # private path streams via stream_vision_reply — the error
        # surfaces there, and the reply stays provider-neutral.
        with mock.patch.object(ron_vision, "fetch_photo_b64",
                               return_value="QUJD"), \
             mock.patch.object(
                 ron_daemon, "stream_vision_reply",
                 side_effect=ron_ai.RateLimitedError("rate limited")):
            self._call()
        self.rich.assert_called_once()
        text = self.rich.call_args[0][1]
        self.assertIn("too many requests", text)
        self.assertNotIn("gemini", text.lower())

    def test_vision_generic_error(self):
        with mock.patch.object(ron_vision, "fetch_photo_b64",
                               return_value="QUJD"), \
             mock.patch.object(ron_daemon, "stream_vision_reply",
                               side_effect=ValueError("weird")):
            self._call()
        self.assertIn("Couldn't read that image",
                      self.rich.call_args[0][1])

    def test_vision_failures_skip_brain_counter(self):
        # A vision-only outage must not push text chat into degraded mode.
        with mock.patch.object(ron_vision, "fetch_photo_b64",
                               return_value="QUJD"), \
             mock.patch.object(
                 ron_daemon, "stream_vision_reply",
                 side_effect=ron_ai.RateLimitedError("rate limited")), \
             mock.patch.object(ron_daemon, "_record_brain_failure") as rbf:
            self._call()
        rbf.assert_not_called()


if __name__ == "__main__":
    unittest.main()
