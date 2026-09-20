#!/usr/bin/env python3
"""Offline tests for /factcheck and /think (no network).

Run: python3 tests/test_spotcmds.py  (or python3 -m unittest discover tests)

Covers:
- ron_cmdquota: per-scope budgets, owner exempt, per-user isolation,
  window expiry, check-before-burn ordering.
- ron_daemon._cmd_arg: /cmd prefix stripping incl. @botname.
- ron_ai.single_shot: pinned provider/model plan, message pass-through,
  no-key and unknown-provider errors, empty-reply error.
- ron_search: DDG lite parsing (uddg unwrap, snippet clean), source
  formatting, web_search backend preference and failure.
- ron_daemon._factcheck_verdict: Perplexity when keyed; otherwise
  search + Groq verdict with daemon-appended sources; Gemini fallback.
- ron_daemon handlers: empty-query hint, quota notice without provider
  burn, success path (ack sent then deleted, pinned provider called,
  quota recorded only on success, history saved).
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

    def test_no_perplexity_key_uses_search_path(self):
        # No Perplexity key -> free path: web search + Groq verdict, NOT
        # a "not connected" refusal. Nothing may burn without quota.
        self._key(None)
        fake_results = [{"title": "T1", "url": "https://example.com/a",
                         "snippet": "s1"}]
        with mock.patch.object(
                ron_daemon.ron_search, "web_search",
                return_value=fake_results) as ws, \
             mock.patch.object(
                ron_daemon.ron_ai, "single_shot",
                return_value="**Verdict:** True") as ss:
            self.assertTrue(ron_daemon._handle_factcheck(
                1, USER_A, "u", "/factcheck x", True))
            ws.assert_called_once_with("x")
            (prov, model, messages), _ = ss.call_args
            self.assertEqual(prov, "groq")
        self.assertTrue(any("**Verdict:** True" in s for s in self.sent))
        self.assertTrue(any("example.com" in s for s in self.sent))

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


_DDG_FIXTURE = """
<tr>
  <td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage%3Fx%3D1&amp;rut=abc" class='result-link'>Example <b>Title</b> [x]</a></td>
</tr>
<tr><td class='result-snippet'>A snippet &amp; more <b>bold</b> text.</td></tr>
<tr>
  <td><a rel="nofollow" href="https://plain.example.org/other" class='result-link'>Plain Link</a></td>
</tr>
"""


class TestRonSearch(unittest.TestCase):
    def test_unwrap_ddg_redirect(self):
        import ron_search
        u = ron_search._unwrap_ddg(
            "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=1")
        self.assertEqual(u, "https://example.com/a")

    def test_unwrap_protocol_relative(self):
        import ron_search
        self.assertEqual(ron_search._unwrap_ddg("//example.com/a"),
                         "https://example.com/a")

    def test_parse_ddg_lite(self):
        import ron_search
        rows = ron_search._parse_ddg_lite(_DDG_FIXTURE)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["url"], "https://example.com/page?x=1")
        self.assertEqual(rows[0]["title"], "Example Title [x]")
        self.assertEqual(rows[0]["snippet"], "A snippet & more bold text.")
        self.assertEqual(rows[1]["url"], "https://plain.example.org/other")

    def test_format_sources_sanitizes(self):
        import ron_search
        out = ron_search.format_sources([
            {"title": "A [b] (c) title", "url": "https://example.com/p",
             "snippet": ""}])
        self.assertIn("1. [A b c title](https://example.com/p)", out)
        self.assertIn("example.com", out)

    def test_format_evidence_numbered(self):
        import ron_search
        out = ron_search.format_evidence([
            {"title": "T", "url": "https://e.com", "snippet": "S"}])
        self.assertIn("[1] T", out)
        self.assertIn("https://e.com", out)

    def test_web_search_empty_query(self):
        import ron_search
        with self.assertRaises(RuntimeError):
            ron_search.web_search("   ")

    def test_web_search_all_fail(self):
        import ron_search
        with mock.patch.object(ron_search, "_tavily_search",
                               return_value=None), \
             mock.patch.object(ron_search, "_ddg_search",
                               return_value=None):
            with self.assertRaises(RuntimeError):
                ron_search.web_search("x")

    def test_web_search_prefers_tavily(self):
        import ron_search
        tav = [{"title": "T", "url": "https://t.co", "snippet": "S"}]
        with mock.patch.object(ron_search, "_tavily_search",
                               return_value=tav), \
             mock.patch.object(ron_search, "_ddg_search") as ddg:
            self.assertEqual(ron_search.web_search("x"), tav)
            ddg.assert_not_called()


class TestFactcheckVerdict(unittest.TestCase):
    def _key(self, val):
        p = mock.patch.object(ron_daemon.ron_ai, "_provider_key",
                              return_value=val)
        p.start()
        self.addCleanup(p.stop)

    def _results(self):
        return [{"title": "T1", "url": "https://example.com/a",
                 "snippet": "s1"},
                {"title": "T2", "url": "https://other.org/b",
                 "snippet": "s2"}]

    def test_perplexity_when_keyed(self):
        self._key("hsurr:fake")
        with mock.patch.object(
                ron_daemon.ron_ai, "single_shot",
                return_value="**Verdict:** True") as ss, \
             mock.patch.object(ron_daemon.ron_search, "web_search") as ws:
            out = ron_daemon._factcheck_verdict("some claim")
        (prov, model, messages), _ = ss.call_args
        self.assertEqual((prov, model), ("perplexity", "sonar-pro"))
        ws.assert_not_called()  # no search needed
        self.assertIn("**Verdict:** True", out)
        self.assertNotIn("Sources:", out)  # perplexity cites inline

    def test_search_plus_groq_verdict(self):
        self._key(None)
        with mock.patch.object(
                ron_daemon.ron_search, "web_search",
                return_value=self._results()) as ws, \
             mock.patch.object(
                ron_daemon.ron_ai, "single_shot",
                return_value="**Verdict:** False") as ss:
            out = ron_daemon._factcheck_verdict("some claim")
        ws.assert_called_once_with("some claim")
        (prov, model, messages), _ = ss.call_args
        self.assertEqual(prov, "groq")
        self.assertEqual(model, "openai/gpt-oss-120b")
        # evidence reaches the model
        self.assertIn("example.com/a", messages[1]["content"])
        self.assertIn("other.org/b", messages[1]["content"])
        # daemon appends real source links
        self.assertIn("**Verdict:** False", out)
        self.assertIn("[T1](https://example.com/a)", out)
        self.assertIn("[T2](https://other.org/b)", out)

    def test_groq_failure_falls_back_to_gemini(self):
        self._key(None)
        with mock.patch.object(ron_daemon.ron_search, "web_search",
                               return_value=self._results()), \
             mock.patch.object(
                ron_daemon.ron_ai, "single_shot",
                side_effect=[RuntimeError("429"),
                             "**Verdict:** Misleading"]) as ss:
            out = ron_daemon._factcheck_verdict("some claim")
        self.assertEqual(ss.call_count, 2)
        self.assertEqual(ss.call_args[0][0], "gemini")
        self.assertIn("**Verdict:** Misleading", out)

    def test_search_failure_raises(self):
        self._key(None)
        with mock.patch.object(ron_daemon.ron_search, "web_search",
                               side_effect=RuntimeError("down")), \
             mock.patch.object(ron_daemon.ron_ai, "single_shot") as ss:
            with self.assertRaises(RuntimeError):
                ron_daemon._factcheck_verdict("some claim")
            ss.assert_not_called()  # no verdict without evidence


class TestOwnerQuotaPing(unittest.TestCase):
    def setUp(self):
        ron_daemon._quota_ping_ts.clear()
        p = mock.patch.object(ron_daemon, "send")
        self.send = p.start()
        self.addCleanup(p.stop)
        self.addCleanup(ron_daemon._quota_ping_ts.clear)

    def test_first_block_pings_owner(self):
        ron_daemon._notify_owner_quota_block(
            "/factcheck", USER_A, "Testy", 10, 10, 6 * 3600)
        self.send.assert_called_once()
        chat_id, text = self.send.call_args[0][:2]
        self.assertEqual(chat_id, OWNER)
        self.assertIn("/factcheck", text)
        self.assertIn("10/10", text)
        self.assertIn("6h", text)

    def test_second_block_same_window_stays_quiet(self):
        ron_daemon._notify_owner_quota_block(
            "/factcheck", USER_A, "Testy", 10, 10, 6 * 3600)
        ron_daemon._notify_owner_quota_block(
            "/factcheck", USER_A, "Testy", 10, 10, 6 * 3600)
        self.send.assert_called_once()  # throttled

    def test_different_scope_still_pings(self):
        ron_daemon._notify_owner_quota_block(
            "/factcheck", USER_A, "Testy", 10, 10, 6 * 3600)
        ron_daemon._notify_owner_quota_block(
            "/think", USER_A, "Testy", 15, 15, 6 * 3600)
        self.assertEqual(self.send.call_count, 2)

    def test_owner_never_pinged(self):
        ron_daemon._notify_owner_quota_block(
            "/factcheck", OWNER, "Adwaith", 0, 10, 6 * 3600)
        self.send.assert_not_called()

    def test_send_failure_does_not_raise(self):
        self.send.side_effect = RuntimeError("net down")
        ron_daemon._notify_owner_quota_block(
            "/factcheck", USER_A, "Testy", 10, 10, 6 * 3600)
        # no exception — the user flow must never break on a ping


class TestCmdQuotaUsage(unittest.TestCase):
    def setUp(self):
        _mk_quota_file(self)

    def test_usage_counts_window(self):
        for _ in range(3):
            ron_cmdquota.record_use("factcheck", USER_A, OWNER, 10, 6 * 3600)
        self.assertEqual(
            ron_cmdquota.usage("factcheck", USER_A, OWNER, 10, 6 * 3600), 3)

    def test_usage_owner_zero(self):
        self.assertEqual(
            ron_cmdquota.usage("factcheck", OWNER, OWNER, 10, 6 * 3600), 0)

    def test_usage_scope_isolation(self):
        ron_cmdquota.record_use("factcheck", USER_A, OWNER, 10, 6 * 3600)
        self.assertEqual(
            ron_cmdquota.usage("think", USER_A, OWNER, 15, 6 * 3600), 0)


if __name__ == "__main__":
    unittest.main()
