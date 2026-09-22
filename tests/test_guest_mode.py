#!/usr/bin/env python3
"""Guest Mode + smart one-shot answers (v1.24.0). No network, no Telegram.

Bot API 10.0 Guest Mode: the bot is @mentioned in a chat it isn't a member
of. Telegram delivers a `guest_message` update carrying `guest_query_id`;
the ONLY write allowed is `answerGuestQuery` — never sendMessage there.

Covers:
  ron_ai.quick_answer:
  1. Empty query raises RuntimeError("empty query").
  2. One-shot request: cheap=True, timeout=30, single user turn, no
     history/memory/cache; reply truncated to MAX_REPLY.
  3. Empty backend reply raises RuntimeError.
  Daemon handlers:
  4. handle_guest_message without guest_query_id -> no API call.
  5. @mention stripped from the tagged text; quick_answer gets the clean
     query; answerGuestQuery carries guest_query_id + the smart answer.
  6. Banned caller -> silence, no API call.
  7. 6 rapid calls from one caller -> 6th gets the slowdown card.
  8. handle_inline_query answers smartly via answerInlineQuery.
  9. AI failure on inline -> degraded echo fallback card (never broken).
  10. Trailers are stripped from one-shot answers.

Run: python3 tests/test_guest_mode.py (from the repo root).
"""
import importlib.util as _ilu
import json
import os
import sys
import unittest
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_daemon

# Hermetic copy of THIS repo's ron_ai (same rationale as test_user_stop.py:
# importing ron_daemon may pull the live dir's ron_ai into sys.modules).
_spec = _ilu.spec_from_file_location(
    "ron_ai_guest_test",
    os.path.join(os.path.dirname(__file__), "..", "ron_ai.py"))
ron_ai = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(ron_ai)


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class QuickAnswerTests(unittest.TestCase):
    def setUp(self):
        self.req_kwargs = {}
        self.reply_text = "Paris is the capital of France."

        def fake_request(cfg, messages, stream, timeout, tier="standard"):
            self.req_kwargs = {"stream": stream, "timeout": timeout,
                               "tier": tier, "n_messages": len(messages),
                               "system": messages[0]["content"],
                               "user": messages[1]["content"]}
            return FakeResp({"choices": [{"message":
                                          {"content": self.reply_text}}]})

        self.orig_request = ron_ai._request
        self.orig_config = ron_ai.load_config
        ron_ai._request = fake_request
        ron_ai.load_config = lambda: {"system": "SYS ",
                                      "providers": []}

    def tearDown(self):
        ron_ai._request = self.orig_request
        ron_ai.load_config = self.orig_config

    def test_empty_query_raises(self):
        for q in ("", "   ", None):
            with self.assertRaisesRegex(RuntimeError, "empty query"):
                ron_ai.quick_answer(q)

    def test_one_shot_shape(self):
        out = ron_ai.quick_answer("capital of France?", "Guest")
        self.assertEqual(out, self.reply_text)
        k = self.req_kwargs
        self.assertEqual(k["tier"], "cheap")  # small model first
        self.assertEqual(k["timeout"], 30)     # clients won't wait around
        self.assertFalse(k["stream"])
        self.assertEqual(k["n_messages"], 2)   # system + one user turn
        self.assertIn("capital of France?", k["user"])
        self.assertIn("Guest asks:", k["user"])
        self.assertIn("SYS ", k["system"])     # full behavior rules kept

    def test_empty_backend_reply_raises(self):
        self.reply_text = "  "
        with self.assertRaisesRegex(RuntimeError, "empty reply"):
            ron_ai.quick_answer("hello?")

    def test_truncates_to_max_reply(self):
        self.reply_text = "x" * (ron_ai.MAX_REPLY + 500)
        self.assertEqual(len(ron_ai.quick_answer("hi")), ron_ai.MAX_REPLY)


class FakeAI:
    def __init__(self):
        self.calls = []
        self.fail = False

    def quick_answer(self, query, user_name="there"):
        self.calls.append((query, user_name))
        if self.fail:
            raise RuntimeError("AI backend returned empty reply")
        return "SMART ANSWER [links: L -> https://example.com]"


def _guest(qid="gq1", text="@ronrebootedbot what is 2+2?", uid=111):
    return {"guest_query_id": qid, "text": text,
            "guest_bot_caller_user": {"id": uid, "first_name": "Guest"}}


def _iq(iid="iq1", query="capital of France?", uid=222):
    return {"id": iid, "query": query,
            "from": {"id": uid, "first_name": "Inliner"}}


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.api_calls = []
        self.orig_api = ron_daemon.api
        ron_daemon.api = (lambda method, params, **k:
                          self.api_calls.append((method, params))
                          or {"ok": True})
        self.fake_ai = FakeAI()
        self.orig_ai = ron_daemon.ron_ai
        ron_daemon.ron_ai = self.fake_ai
        self.orig_banned = ron_daemon.ron_owner.is_banned
        ron_daemon.ron_owner.is_banned = lambda uid: uid == 999
        ron_daemon._ONE_SHOT_THROTTLE.clear()

    def tearDown(self):
        ron_daemon.api = self.orig_api
        ron_daemon.ron_ai = self.orig_ai
        ron_daemon.ron_owner.is_banned = self.orig_banned
        ron_daemon._ONE_SHOT_THROTTLE.clear()

    def _last(self, method):
        calls = [p for m, p in self.api_calls if m == method]
        self.assertTrue(calls, f"no {method} call")
        return calls[-1]

    # -- guest -----------------------------------------------------------
    def test_guest_no_query_id_no_call(self):
        ron_daemon.handle_guest_message({"text": "@ronrebootedbot hi"})
        self.assertEqual(self.api_calls, [])

    def test_guest_mention_stripped_and_answered(self):
        ron_daemon.handle_guest_message(_guest())
        params = self._last("answerGuestQuery")
        self.assertEqual(params["guest_query_id"], "gq1")
        result = json.loads(params["result"])
        self.assertEqual(result["title"], "Ron's answer")
        body = result["input_message_content"]["message_text"]
        self.assertIn("SMART ANSWER", body)
        self.assertNotIn("[links:", body)  # trailer stripped
        # quick_answer got the clean query + caller name
        self.assertEqual(self.fake_ai.calls, [("what is 2+2?", "Guest")])

    def test_guest_banned_caller_silence(self):
        ron_daemon.handle_guest_message(_guest(uid=999))
        self.assertEqual(self.api_calls, [])
        self.assertEqual(self.fake_ai.calls, [])

    def test_guest_throttle_slowdown_card(self):
        for i in range(5):
            ron_daemon.handle_guest_message(_guest(qid=f"g{i}"))
        self.assertEqual(len(self.fake_ai.calls), 5)
        ron_daemon.handle_guest_message(_guest(qid="g5"))
        self.assertEqual(len(self.fake_ai.calls), 5)  # no 6th AI call
        params = self._last("answerGuestQuery")
        result = json.loads(params["result"])
        self.assertEqual(result["id"], "slowdown")

    def test_guest_ai_failure_falls_back_to_info_card(self):
        self.fake_ai.fail = True
        ron_daemon.handle_guest_message(_guest())
        params = self._last("answerGuestQuery")
        result = json.loads(params["result"])
        self.assertEqual(result["id"], "openron")

    def test_guest_bare_mention_gets_howto_card(self):
        ron_daemon.handle_guest_message(_guest(text="@ronrebootedbot"))
        params = self._last("answerGuestQuery")
        result = json.loads(params["result"])
        self.assertEqual(result["id"], "openron")
        self.assertEqual(self.fake_ai.calls, [])

    # -- inline ----------------------------------------------------------
    def test_inline_no_id_no_call(self):
        ron_daemon.handle_inline_query({"query": "hi"})
        self.assertEqual(self.api_calls, [])

    def test_inline_smart_answer(self):
        ron_daemon.handle_inline_query(_iq())
        params = self._last("answerInlineQuery")
        self.assertEqual(params["inline_query_id"], "iq1")
        results = json.loads(params["results"])
        self.assertEqual(results[0]["id"], "askron")
        self.assertEqual(results[0]["title"], "Ron's answer")
        self.assertIn("SMART ANSWER",
                      results[0]["input_message_content"]["message_text"])
        self.assertEqual(results[1]["id"], "openron")  # card kept
        self.assertEqual(self.fake_ai.calls, [("capital of France?",
                                               "Inliner")])

    def test_inline_ai_failure_echo_fallback(self):
        self.fake_ai.fail = True
        ron_daemon.handle_inline_query(_iq())
        params = self._last("answerInlineQuery")
        results = json.loads(params["results"])
        echo = results[0]
        self.assertEqual(echo["id"], "askron")
        self.assertEqual(echo["input_message_content"]["message_text"],
                         "capital of France?")

    def test_inline_throttle(self):
        for i in range(6):
            ron_daemon.handle_inline_query(_iq(iid=f"i{i}"))
        params = self._last("answerInlineQuery")
        results = json.loads(params["results"])
        self.assertEqual(results[0]["id"], "slowdown")

    def test_inline_empty_query_just_open_card(self):
        ron_daemon.handle_inline_query(_iq(query="   "))
        params = self._last("answerInlineQuery")
        results = json.loads(params["results"])
        self.assertEqual([r["id"] for r in results], ["openron"])
        self.assertEqual(self.fake_ai.calls, [])


class ApiEncodingTests(unittest.TestCase):
    """api() must JSON-serialize list/dict params (v1.24.0).

    Regression: urlencode() turned allowed_updates into the Python repr
    "['message', ...]", which Telegram can't parse as JSON and silently
    ignores — the daemon's update subscription could never actually
    change (this is why guest_message never appeared server-side).
    """

    def _call(self, params):
        captured = {}

        class FakePool:
            def post(self, path, body, content_type, timeout=65,
                     extra_headers=None):
                captured["body"] = body
                return (200, b'{"ok": true, "result": []}')

        orig = ron_daemon.http_pool.pool_for
        ron_daemon.http_pool.pool_for = lambda host: FakePool()
        try:
            return ron_daemon.api("getUpdates", params), captured
        finally:
            ron_daemon.http_pool.pool_for = orig

    def test_list_param_is_json_array(self):
        r, captured = self._call(
            {"offset": 1, "timeout": 50,
             "allowed_updates": ["message", "guest_message"]})
        self.assertTrue(r["ok"])
        qs = urllib.parse.parse_qs(captured["body"].decode())
        self.assertEqual(json.loads(qs["allowed_updates"][0]),
                         ["message", "guest_message"])
        self.assertEqual(qs["offset"][0], "1")  # scalars untouched

    def test_dict_param_is_json_object(self):
        r, captured = self._call({"chat_id": 5,
                                  "reply_markup": {"inline_keyboard": []}})
        self.assertTrue(r["ok"])
        qs = urllib.parse.parse_qs(captured["body"].decode())
        self.assertEqual(json.loads(qs["reply_markup"][0]),
                         {"inline_keyboard": []})

    def test_no_python_repr_leaks(self):
        _, captured = self._call({"allowed_updates": ["message"]})
        body = captured["body"].decode()
        self.assertNotIn("%27", body)  # %27 = ' — Python repr quotes


if __name__ == "__main__":
    unittest.main(verbosity=2)
