#!/usr/bin/env python3
"""Offline tests for /factcheck and /think (no network).

Run: python3 tests/test_spotcmds.py  (or python3 -m unittest discover tests)

Covers:
- ron_cmdquota: per-scope budgets, owner exempt, per-user isolation,
  window expiry, check-before-burn ordering.
- ron_daemon._cmd_arg: /cmd prefix stripping incl. @botname.
- ron_ai.single_shot: pinned provider/model plan, message pass-through,
  no-key and unknown-provider errors, empty-reply error.
- ron_daemon handlers: empty-query hint, no-key notice without provider
  burn, quota notice without provider burn, success path (ack sent then
  deleted, pinned provider called, quota recorded only on success,
  history saved).
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_ai
import ron_cmdquota
import ron_daemon

OWNER = ron_daemon.OWNER_ID
USER_A = 111111
USER_B = 222222


def _mk_quota_file(test):
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    test.addCleanup(os.unlink, tmp.name)
    p = mock.patch.object(ron_cmdquota, "_LIMIT_FILE", tmp.name)
    p.start()
    test.addCleanup(p.stop)
    return tmp.name


class SyncThread:
    """threading.Thread stand-in that runs the target inline."""
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args or ()
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return False


class TestCmdQuota(unittest.TestCase):
    def setUp(self):
        _mk_quota_file(self)

    def test_owner_exempt(self):
        for _ in range(50):
            ron_cmdquota.record_use("factcheck", OWNER, OWNER, 10, 3600)
        allowed, retry = ron_cmdquota.check_limit(
            "factcheck", OWNER, OWNER, 10, 3600)
        self.assertTrue(allowed)
        self.assertEqual(retry, 0.0)

    def test_n_allowed_then_blocked(self):
        for _ in range(10):
            allowed, _ = ron_cmdquota.check_limit(
                "factcheck", USER_A, OWNER, 10, 6 * 3600)
            self.assertTrue(allowed)
            ron_cmdquota.record_use("factcheck", USER_A, OWNER, 10, 6 * 3600)
        allowed, retry = ron_cmdquota.check_limit(
            "factcheck", USER_A, OWNER, 10, 6 * 3600)
        self.assertFalse(allowed)
        self.assertGreater(retry, 0.0)

    def test_scope_isolation(self):
        for _ in range(10):
            ron_cmdquota.record_use("factcheck", USER_A, OWNER, 10, 6 * 3600)
        allowed, _ = ron_cmdquota.check_limit(
            "think", USER_A, OWNER, 15, 6 * 3600)
        self.assertTrue(allowed)

    def test_per_user_isolation(self):
        for _ in range(10):
            ron_cmdquota.record_use("factcheck", USER_A, OWNER, 10, 6 * 3600)
        allowed, _ = ron_cmdquota.check_limit(
            "factcheck", USER_B, OWNER, 10, 6 * 3600)
        self.assertTrue(allowed)

    def test_window_expiry(self):
        with mock.patch("time.time", return_value=1_000_000.0):
            for _ in range(10):
                ron_cmdquota.record_use(
                    "factcheck", USER_A, OWNER, 10, 6 * 3600)
        with mock.patch("time.time", return_value=1_000_000.0 + 7 * 3600):
            allowed, _ = ron_cmdquota.check_limit(
                "factcheck", USER_A, OWNER, 10, 6 * 3600)
            self.assertTrue(allowed)

    def test_check_does_not_consume(self):
        for _ in range(30):
            allowed, _ = ron_cmdquota.check_limit(
                "factcheck", USER_A, OWNER, 10, 6 * 3600)
            self.assertTrue(allowed)


class TestCmdArg(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(
            ron_daemon._cmd_arg("/factcheck the moon is cheese"),
            "the moon is cheese")

    def test_botname_suffix(self):
        self.assertEqual(
            ron_daemon._cmd_arg("/think@ronrebootedbot why sky blue"),
            "why sky blue")

    def test_bare_command(self):
        self.assertEqual(ron_daemon._cmd_arg("/factcheck"), "")
        self.assertEqual(ron_daemon._cmd_arg("/think   "), "")

    def test_empty(self):
        self.assertEqual(ron_daemon._cmd_arg(""), "")
        self.assertEqual(ron_daemon._cmd_arg(None), "")


class TestSingleShot(unittest.TestCase):
    def test_pins_provider_and_model(self):
        seen = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {"choices": [{"message": {"content": "verd: true"}}]}
                ).encode()

        def fake_request(cfg, messages, stream, timeout, cheap=False):
            seen["cfg"] = cfg
            seen["messages"] = messages
            seen["stream"] = stream
            return FakeResp()

        with mock.patch.object(ron_ai, "_provider_key",
                               return_value="hsurr:fake"), \
             mock.patch.object(ron_ai, "_request", fake_request):
            out = ron_ai.single_shot(
                "perplexity", "sonar-pro",
                [{"role": "user", "content": "hi"}])
        self.assertEqual(out, "verd: true")
        self.assertEqual(seen["cfg"]["providers"],
                         [{"name": "perplexity", "models": ["sonar-pro"]}])
        self.assertEqual(seen["messages"],
                         [{"role": "user", "content": "hi"}])
        self.assertFalse(seen["stream"])

    def test_no_key_raises(self):
        with mock.patch.object(ron_ai, "_provider_key",
                               return_value=None):
            with self.assertRaisesRegex(RuntimeError, "not connected"):
                ron_ai.single_shot("perplexity", "sonar-pro", [])

    def test_unknown_provider_raises(self):
        with self.assertRaisesRegex(RuntimeError, "unknown AI provider"):
            ron_ai.single_shot("nope", "m", [])

    def test_empty_reply_raises(self):
        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {"choices": [{"message": {"content": "  "}}]}).encode()

        with mock.patch.object(ron_ai, "_provider_key",
                               return_value="hsurr:fake"), \
             mock.patch.object(ron_ai, "_request",
                               return_value=FakeResp()):
            with self.assertRaisesRegex(RuntimeError, "empty reply"):
                ron_ai.single_shot("gemini", "gemini-3.8-flash", [])


class _HandlerBase(unittest.TestCase):
    def setUp(self):
        _mk_quota_file(self)
        p = mock.patch("threading.Thread", SyncThread)
        p.start()
        self.addCleanup(p.stop)
        self.sent = []
        self.api_calls = []

        def fake_send(chat_id, text, reply_to=None, thread_id=None,
                      business_connection_id=None):
            self.sent.append(text)
            return {"ok": True, "result": {"message_id": 42}}

        def fake_api(method, params):
            self.api_calls.append((method, params))
            return {"ok": True}

        p1 = mock.patch.object(ron_daemon, "send", fake_send)
        p1.start()
        self.addCleanup(p1.stop)
        p2 = mock.patch.object(ron_daemon, "api", fake_api)
        p2.start()
        self.addCleanup(p2.stop)
        p3 = mock.patch.object(ron_daemon, "send_rich_with_kb",
                               lambda c, t, **k: self.sent.append(t))
        p3.start()
        self.addCleanup(p3.stop)


class TestFactcheckHandler(_HandlerBase):
    def _key(self, val):
        p = mock.patch.object(ron_daemon.ron_ai, "_provider_key",
                              return_value=val)
        p.start()
        self.addCleanup(p.stop)

    def test_empty_query_hint(self):
        self._key("hsurr:fake")
        self.assertTrue(ron_daemon._handle_factcheck(
            1, USER_A, "u", "/factcheck", True))
        self.assertTrue(any("claim to check" in s for s in self.sent))

    def test_no_key_no_burn(self):
        self._key(None)
        with mock.patch.object(
                ron_daemon.ron_ai, "single_shot") as ss:
            self.assertTrue(ron_daemon._handle_factcheck(
                1, USER_A, "u", "/factcheck x", True))
            ss.assert_not_called()
        self.assertTrue(any("isn't connected" in s for s in self.sent))

    def test_quota_blocks_without_burn(self):
        self._key("hsurr:fake")
        for _ in range(10):
            ron_cmdquota.record_use("factcheck", USER_A, OWNER, 10, 6 * 3600)
        with mock.patch.object(
                ron_daemon.ron_ai, "single_shot") as ss:
            self.assertTrue(ron_daemon._handle_factcheck(
                1, USER_A, "u", "/factcheck x", True))
            ss.assert_not_called()
        self.assertTrue(any("limit" in s for s in self.sent))

    def test_success_path(self):
        self._key("hsurr:fake")
        with mock.patch.object(
                ron_daemon.ron_ai, "single_shot",
                return_value="**Verdict:** True") as ss, \
             mock.patch.object(ron_daemon.ron_ai, "_save_turn") as st:
            self.assertTrue(ron_daemon._handle_factcheck(
                1, USER_A, "u", "/factcheck the sky is blue", True))
        # pinned provider, right model, system prompt present
        (prov, model, messages), kwargs = ss.call_args
        self.assertEqual(prov, "perplexity")
        self.assertEqual(model, "sonar-pro")
        self.assertIn("fact-checker", messages[0]["content"])
        self.assertEqual(messages[1]["content"], "the sky is blue")
        # ack sent then deleted; answer delivered; quota spent; history kept
        self.assertTrue(any("Fact-checking" in s for s in self.sent))
        self.assertIn(("deleteMessage",
                       {"chat_id": 1, "message_id": 42}), self.api_calls)
        self.assertIn("**Verdict:** True", self.sent)
        allowed, _ = ron_cmdquota.check_limit(
            "factcheck", USER_A, OWNER, 10, 6 * 3600)
        self.assertTrue(allowed)  # 1 of 10 used
        st.assert_called_once()
        saved_user = st.call_args[0][1]
        self.assertIn("/factcheck", saved_user)

    def test_failure_path_no_quota_burn(self):
        self._key("hsurr:fake")
        with mock.patch.object(
                ron_daemon.ron_ai, "single_shot",
                side_effect=RuntimeError("boom")):
            self.assertTrue(ron_daemon._handle_factcheck(
                1, USER_A, "u", "/factcheck x", True))
        self.assertTrue(any("hiccuped" in s for s in self.sent))
        allowed, _ = ron_cmdquota.check_limit(
            "factcheck", USER_A, OWNER, 10, 6 * 3600)
        self.assertTrue(allowed)  # failures never consume quota


class TestThinkHandler(_HandlerBase):
    def _key(self, val):
        p = mock.patch.object(ron_daemon.ron_ai, "_provider_key",
                              return_value=val)
        p.start()
        self.addCleanup(p.stop)

    def test_empty_query_hint(self):
        self._key("hsurr:fake")
        self.assertTrue(ron_daemon._handle_think(
            1, USER_A, "u", "/think", True))
        self.assertTrue(any("something to think" in s for s in self.sent))

    def test_no_key_no_burn(self):
        self._key(None)
        with mock.patch.object(
                ron_daemon.ron_ai, "single_shot") as ss:
            self.assertTrue(ron_daemon._handle_think(
                1, USER_A, "u", "/think x", True))
            ss.assert_not_called()

    def test_quota_blocks_without_burn(self):
        self._key("hsurr:fake")
        for _ in range(15):
            ron_cmdquota.record_use("think", USER_A, OWNER, 15, 6 * 3600)
        with mock.patch.object(
                ron_daemon.ron_ai, "single_shot") as ss:
            self.assertTrue(ron_daemon._handle_think(
                1, USER_A, "u", "/think x", True))
            ss.assert_not_called()
        self.assertTrue(any("limit" in s for s in self.sent))

    def test_success_pins_gemini(self):
        self._key("hsurr:fake")
        with mock.patch.object(
                ron_daemon.ron_ai, "single_shot",
                return_value="deep answer") as ss, \
             mock.patch.object(ron_daemon.ron_ai, "_save_turn"):
            self.assertTrue(ron_daemon._handle_think(
                1, USER_A, "u", "/think why do we dream", True))
        (prov, model, messages), kwargs = ss.call_args
        self.assertEqual(prov, "gemini")
        self.assertEqual(model, "gemini-3.8-flash")
        self.assertIn("deep-thinking", messages[0]["content"])
        self.assertIn("deep answer", self.sent)


if __name__ == "__main__":
    unittest.main()
