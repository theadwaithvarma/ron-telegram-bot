#!/usr/bin/env python3
"""Offline tests for image generation /imagine (no network).

Run: python3 tests/test_imagen.py  (or python3 -m unittest discover tests)

Covers:
- ron_imagen rate limit: 10 images / rolling 6h, owner exempt, per-user
  isolation, window expiry, check-before-fetch ordering (failed fetches
  never consume quota).
- ron_imagen.build_url: prompt URL-encoded, size/nologo/seed params,
  prompt truncation.
- ron_imagen.fetch_image: JPEG validation, temp cleanup on failure,
  empty prompt rejected.
- ron_imagen.send_photo: multipart body shape, caption cap.
- ron_daemon /imagine hook: prompt parsing, empty-prompt hint, limit
  notice without any provider burn, ack sent then deleted, quota
  recorded only on success, history marker without image bytes.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_daemon
import ron_imagen

OWNER = ron_daemon.OWNER_ID
USER_A = 111111
USER_B = 222222

FAKE_JPEG = b"\xff\xd8\xff" + b"\x00" * 5000 + b"\xff\xd9"


def _mk_limits_file(test):
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    test.addCleanup(os.unlink, tmp.name)
    p = mock.patch.object(ron_imagen, "LIMIT_FILE", tmp.name)
    p.start()
    test.addCleanup(p.stop)
    return tmp.name


class SyncThread:
    """threading.Thread stand-in that runs the target inline."""
    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


class TestImagenLimits(unittest.TestCase):
    def setUp(self):
        _mk_limits_file(self)

    def test_owner_exempt(self):
        for _ in range(50):
            ron_imagen.record_use(OWNER, OWNER)
        allowed, retry = ron_imagen.check_limit(OWNER, OWNER)
        self.assertTrue(allowed)
        self.assertEqual(retry, 0.0)

    def test_ten_allowed_eleventh_blocked(self):
        for _ in range(10):
            allowed, _ = ron_imagen.check_limit(USER_A, OWNER)
            self.assertTrue(allowed)
            ron_imagen.record_use(USER_A, OWNER)
        allowed, retry = ron_imagen.check_limit(USER_A, OWNER)
        self.assertFalse(allowed)
        self.assertGreater(retry, 0.0)
        self.assertLessEqual(retry, 6 * 3600)

    def test_check_does_not_consume(self):
        for _ in range(30):
            allowed, _ = ron_imagen.check_limit(USER_A, OWNER)
            self.assertTrue(allowed)

    def test_window_expiry(self):
        old = 7 * 3600  # older than the 6h window
        with mock.patch("time.time", return_value=1_000_000.0):
            for _ in range(10):
                ron_imagen.record_use(USER_A, OWNER)
        with mock.patch("time.time", return_value=1_000_000.0 + old):
            allowed, _ = ron_imagen.check_limit(USER_A, OWNER)
            self.assertTrue(allowed)

    def test_per_user_isolation(self):
        for _ in range(10):
            ron_imagen.record_use(USER_A, OWNER)
        allowed, _ = ron_imagen.check_limit(USER_B, OWNER)
        self.assertTrue(allowed)
        allowed, _ = ron_imagen.check_limit(USER_A, OWNER)
        self.assertFalse(allowed)

    def test_owner_uses_not_tracked(self):
        ron_imagen.record_use(OWNER, OWNER)
        with open(ron_imagen.LIMIT_FILE) as f:
            raw = f.read().strip()
        d = json.loads(raw) if raw else {}
        self.assertNotIn(str(OWNER), d)

    def test_stats_counts_24h(self):
        ron_imagen.record_use(USER_A, OWNER)
        ron_imagen.record_use(USER_B, OWNER)
        st = ron_imagen.stats()
        self.assertEqual(st["images_gen_24h"], 2)


class TestBuildUrl(unittest.TestCase):
    def test_prompt_encoded_and_params(self):
        url = ron_imagen.build_url("a red fox in snow")
        self.assertTrue(url.startswith("https://image.pollinations.ai/prompt/"))
        self.assertIn("a%20red%20fox%20in%20snow", url)
        self.assertIn("width=1024", url)
        self.assertIn("height=1024", url)
        self.assertIn("nologo=true", url)
        self.assertIn("seed=", url)

    def test_prompt_truncated(self):
        url = ron_imagen.build_url("x" * 1000)
        path = url.split("?")[0]
        # encoded prompt part stays bounded by MAX_PROMPT_CHARS
        self.assertLessEqual(len(path.split("/prompt/")[1]),
                             ron_imagen.MAX_PROMPT_CHARS * 3)


def _fake_urlopen(data):
    fake_resp = mock.MagicMock()
    fake_resp.read.return_value = data
    m = mock.patch("urllib.request.urlopen")
    urlopen = m.start()
    urlopen.return_value.__enter__.return_value = fake_resp
    return m


class TestFetchImage(unittest.TestCase):
    def test_happy_path_returns_tmp_jpeg(self):
        before = set(os.listdir("/tmp"))
        m = _fake_urlopen(FAKE_JPEG)
        self.addCleanup(m.stop)
        path = ron_imagen.fetch_image("a cat")
        try:
            self.assertTrue(path.startswith("/tmp"))
            with open(path, "rb") as f:
                self.assertEqual(f.read(), FAKE_JPEG)
        finally:
            os.unlink(path)
        self.assertEqual(set(os.listdir("/tmp")), before)

    def test_non_jpeg_rejected_and_cleaned(self):
        before = set(os.listdir("/tmp"))
        m = _fake_urlopen(b"<html>error page</html>" * 100)
        self.addCleanup(m.stop)
        with self.assertRaises(RuntimeError):
            ron_imagen.fetch_image("a cat")
        self.assertEqual(set(os.listdir("/tmp")), before)

    def test_empty_prompt_rejected(self):
        with self.assertRaises(RuntimeError):
            ron_imagen.fetch_image("   ")

    def test_network_error_cleans_tmp(self):
        before = set(os.listdir("/tmp"))
        m = mock.patch("urllib.request.urlopen",
                       side_effect=OSError("down"))
        m.start()
        self.addCleanup(m.stop)
        with self.assertRaises(Exception):
            ron_imagen.fetch_image("a cat")
        self.assertEqual(set(os.listdir("/tmp")), before)


class TestSendPhoto(unittest.TestCase):
    def test_multipart_shape(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.write(FAKE_JPEG)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        seen = {}

        def fake_urlopen(req, *a, **k):
            seen["url"] = req.full_url
            seen["body"] = req.data
            seen["ctype"] = req.get_header("Content-type")
            fake_resp = mock.MagicMock()
            fake_resp.read.return_value = b'{"ok": true, "result": {}}'
            cm = mock.MagicMock()
            cm.__enter__.return_value = fake_resp
            return cm

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            r = ron_imagen.send_photo(777, tmp.name, caption="hi",
                                      reply_to=42, thread_id=7)
        self.assertTrue(r["ok"])
        self.assertIn("sendPhoto", seen["url"])
        self.assertIn("multipart/form-data", seen["ctype"])
        body = seen["body"]
        self.assertIn(b'name="chat_id"', body)
        self.assertIn(b"777", body)
        self.assertIn(b'name="photo"', body)
        self.assertIn(FAKE_JPEG, body)  # image bytes go to Telegram...
        self.assertIn(b'name="caption"', body)
        self.assertIn(b'"message_id": 42', body)
        self.assertIn(b'name="message_thread_id"', body)

    def test_caption_capped_at_1024(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.write(FAKE_JPEG)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        seen = {}

        def fake_urlopen(req, *a, **k):
            seen["body"] = req.data
            fake_resp = mock.MagicMock()
            fake_resp.read.return_value = b'{"ok": true}'
            cm = mock.MagicMock()
            cm.__enter__.return_value = fake_resp
            return cm

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            ron_imagen.send_photo(777, tmp.name, caption="x" * 5000)
        # caption field present but truncated to Telegram's 1024 cap
        self.assertNotIn(b"x" * 5000, seen["body"])
        self.assertIn(b"x" * 1024, seen["body"])


class TestImagineHook(unittest.TestCase):
    def setUp(self):
        _mk_limits_file(self)
        self.thread = mock.patch("threading.Thread", SyncThread).start()
        self.send = mock.patch.object(ron_daemon, "send").start()
        self.rich = mock.patch.object(ron_daemon, "send_rich_with_kb").start()
        self.addCleanup(mock.patch.stopall)
        # send() ack returns a message_id for the delete step
        self.send.return_value = {"ok": True,
                                  "result": {"message_id": 99}}

    def _cmd(self, text, user_id=USER_A, **kw):
        args = dict(chat_id=777, user_id=user_id, cmd="imagine",
                    is_private=True, text=text, user_name="Testy")
        args.update(kw)
        return ron_daemon.handle_command(**args)

    def test_prompt_parsing(self):
        self.assertEqual(ron_daemon._imagine_prompt("/imagine a fox"),
                         "a fox")
        self.assertEqual(
            ron_daemon._imagine_prompt("/imagine@ronrebootedbot a fox"),
            "a fox")
        self.assertEqual(ron_daemon._imagine_prompt("/imagine"), "")

    def test_empty_prompt_hint_no_worker(self):
        with mock.patch.object(ron_daemon, "_ai_imagine_reply") as w:
            self.assertTrue(self._cmd("/imagine"))
        w.assert_not_called()
        self.send.assert_called_once()
        self.assertIn("/imagine", self.send.call_args[0][1])

    def test_limit_blocks_before_provider(self):
        for _ in range(10):
            ron_imagen.record_use(USER_A, OWNER)
        with mock.patch.object(ron_imagen, "fetch_image") as fetch, \
             mock.patch.object(ron_daemon, "_ai_imagine_reply") as w:
            self.assertTrue(self._cmd("/imagine a fox"))
        fetch.assert_not_called()  # zero provider burn
        w.assert_not_called()
        sent = self.send.call_args[0][1]
        self.assertIn("image-generation limit", sent)
        self.assertNotIn("pollinations", sent.lower())

    def test_owner_ignores_limit(self):
        for _ in range(25):
            ron_imagen.record_use(USER_A, OWNER)
        with mock.patch.object(ron_daemon, "_ai_imagine_reply") as w:
            self.assertTrue(self._cmd("/imagine a fox", user_id=OWNER))
        w.assert_called_once()

    def test_module_missing(self):
        with mock.patch.object(ron_daemon, "ron_imagen", None):
            self.assertTrue(self._cmd("/imagine a fox"))
        self.send.assert_called_once()
        self.assertIn("isn't available", self.send.call_args[0][1])

    def test_limit_text_formatting(self):
        self.assertIn("~5h 0m", ron_daemon._imagen_limit_text(5 * 3600 + 30))
        self.assertIn("~3m", ron_daemon._imagen_limit_text(150))


class TestImagineWorker(unittest.TestCase):
    """_ai_imagine_reply: ack -> fetch -> quota -> upload -> history."""
    def setUp(self):
        _mk_limits_file(self)
        self.thread = mock.patch("threading.Thread", SyncThread).start()
        self.send = mock.patch.object(ron_daemon, "send").start()
        self.rich = mock.patch.object(ron_daemon, "send_rich_with_kb").start()
        self.upload = mock.patch.object(ron_imagen, "send_photo").start()
        self.delete = mock.patch.object(ron_imagen, "delete_message").start()
        self.save = mock.patch.object(ron_daemon.ron_ai, "_save_turn").start()
        self.addCleanup(mock.patch.stopall)
        self.send.return_value = {"ok": True,
                                  "result": {"message_id": 99}}
        self.upload.return_value = {"ok": True, "result": {}}
        self.tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        self.tmp.write(FAKE_JPEG)
        self.tmp.close()

    def _call(self, **kw):
        args = dict(chat_id=777, user_id=USER_A, user_name="Testy",
                    prompt="a red fox", private=True)
        args.update(kw)
        ron_daemon._ai_imagine_reply(**args)

    def test_success_flow(self):
        paths = []
        real_fetch = ron_imagen.fetch_image

        def fake_fetch(prompt):
            self.assertEqual(prompt, "a red fox")
            paths.append(self.tmp.name)
            return self.tmp.name

        with mock.patch.object(ron_imagen, "fetch_image", fake_fetch), \
             mock.patch.object(ron_imagen, "record_use") as rec:
            self._call()
        # ack sent, then deleted after upload
        self.send.assert_called_once()
        self.assertIn("Generating", self.send.call_args[0][1])
        self.delete.assert_called_once_with(777, 99)
        # upload got the fetched file, caption trimmed
        self.upload.assert_called_once()
        _, ukw = self.upload.call_args
        self.assertEqual(ukw["caption"], "🎨 a red fox")
        rec.assert_called_once_with(USER_A, OWNER)
        # history marker, never image bytes
        self.save.assert_called_once()
        sargs = self.save.call_args[0]
        self.assertIn("[generated image]", sargs[3])
        self.assertIn("a red fox", sargs[3])
        self.assertNotIn(FAKE_JPEG.decode("latin1"), sargs[3])
        # temp file cleaned up
        self.assertFalse(os.path.exists(self.tmp.name))

    def test_fetch_failure_never_consumes_quota(self):
        with mock.patch.object(ron_imagen, "fetch_image",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(ron_imagen, "record_use") as rec:
            self._call()
        rec.assert_not_called()
        self.upload.assert_not_called()
        self.delete.assert_called_once_with(777, 99)  # ack removed
        self.rich.assert_called_once()
        text = self.rich.call_args[0][1]
        self.assertIn("Couldn't generate", text)
        self.assertNotIn("pollinations", text.lower())

    def test_upload_failure_honest_message(self):
        self.upload.return_value = {"ok": False, "error": "bad"}
        with mock.patch.object(ron_imagen, "fetch_image",
                               return_value=self.tmp.name):
            self._call()
        self.rich.assert_called_once()
        self.assertIn("upload failed", self.rich.call_args[0][1])
        self.assertFalse(os.path.exists(self.tmp.name))


if __name__ == "__main__":
    unittest.main()
