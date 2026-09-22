#!/usr/bin/env python3
"""Offline tests for the keyless Pollinations fallback leg (no network).

2026-09-22 (free-backup): Ron's failover chain gains a keyless last-resort
provider (Pollinations — no signup, no API key) plus a wider OpenRouter
:free rotation. Verifies:
  - a provider with key_slot None is treated as keyless ("", not skipped)
  - keyless providers are included in the attempt plan
  - no Authorization header is sent on keyless requests
  - the keyless leg is reached when every keyed provider fails
  - keyed providers without a vault key are still skipped silently
"""
import importlib.util as _ilu
import json
import os
import sys
import unittest
from unittest import mock

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)
for _m in ("ron_ai", "ron_ai_under_test"):
    sys.modules.pop(_m, None)

_spec = _ilu.spec_from_file_location(
    "ron_ai_keyless_test",
    os.path.join(REPO, "ron_ai.py"))
ron_ai = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(ron_ai)
assert "ron-telegram-bot" in ron_ai.__file__, ron_ai.__file__


class FakeStreamResp:
    def __init__(self, status, body=b""):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def close(self):
        pass


class FakePool:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []  # (host, headers, model)

    def stream(self, method, path, body, headers, timeout):
        model = json.loads(body.decode())["model"]
        self.calls.append((path, dict(headers), model))
        return self.handler(model)


def _cfg():
    return {"providers": [
        {"name": "groq", "models": ["openai/gpt-oss-20b"]},
        {"name": "pollinations", "models": ["openai"]},
    ]}


class KeylessProviderTest(unittest.TestCase):
    def test_key_slot_none_returns_empty_not_none(self):
        with mock.patch.object(ron_ai, "dynamic_credential_entry",
                               side_effect=ron_ai.DynamicCredentialError("x")):
            self.assertEqual(ron_ai._provider_key("pollinations"), "")
            self.assertIsNone(ron_ai._provider_key("groq"))

    def test_keyless_provider_stays_in_plan(self):
        pool = FakePool(lambda model: FakeStreamResp(200))
        with mock.patch.object(ron_ai, "_provider_key",
                               side_effect=lambda n: "" if n == "pollinations"
                               else "KEY"), \
             mock.patch.object(ron_ai.http_pool, "pool_for",
                               return_value=pool):
            resp = ron_ai._request(_cfg(), [{"role": "user",
                                             "content": "hi"}],
                                  stream=False, timeout=5)
            resp.close()
        models = [c[2] for c in pool.calls]
        self.assertIn("openai", models)  # pollinations leg attempted

    def test_no_auth_header_on_keyless_requests(self):
        pool = FakePool(lambda model: FakeStreamResp(200))
        with mock.patch.object(ron_ai, "_provider_key",
                               side_effect=lambda n: "" if n == "pollinations"
                               else None), \
             mock.patch.object(ron_ai.http_pool, "pool_for",
                               return_value=pool):
            resp = ron_ai._request(_cfg(), [{"role": "user",
                                             "content": "hi"}],
                                  stream=False, timeout=5)
            resp.close()
        # groq skipped (no key); only the pollinations call went out
        self.assertEqual(len(pool.calls), 1)
        self.assertNotIn("Authorization", pool.calls[0][1])

    def test_auth_header_present_on_keyed_requests(self):
        host, path, body, headers = ron_ai._build_request(
            "https://x.test", "SECRET", "m",
            [{"role": "user", "content": "hi"}], False, {})
        self.assertEqual(headers["Authorization"], "Bearer SECRET")

    def test_fallback_reaches_keyless_after_keyed_429(self):
        def handler(model):
            if model == "openai/gpt-oss-20b":
                return FakeStreamResp(429, b'{"error":"rate limited"}')
            return FakeStreamResp(200)
        pool = FakePool(handler)
        with mock.patch.object(ron_ai, "_provider_key",
                               side_effect=lambda n: "" if n == "pollinations"
                               else "KEY"), \
             mock.patch.object(ron_ai.http_pool, "pool_for",
                               return_value=pool):
            resp = ron_ai._request(_cfg(), [{"role": "user",
                                             "content": "hi"}],
                                  stream=False, timeout=5)
            resp.close()
        models = [c[2] for c in pool.calls]
        self.assertEqual(models, ["openai/gpt-oss-20b", "openai"])

    def test_default_chain_has_pollinations_last(self):
        names = [p["name"] for p in ron_ai.DEFAULT_PROVIDERS]
        self.assertEqual(names[-1], "pollinations")
        or_models = [p["models"] for p in ron_ai.DEFAULT_PROVIDERS
                     if p["name"] == "openrouter"][0]
        self.assertIn("openrouter/free", or_models)
        self.assertGreaterEqual(len(or_models), 5)

    def test_probe_treats_keyless_as_probeable(self):
        class P:
            def get(self, path, headers, timeout):
                self.got_headers = headers
                return 200, b'{"data":[{"id":"openai"}]}'
        pool = P()
        with mock.patch.object(ron_ai, "_provider_key",
                               side_effect=lambda n: "" if n == "pollinations"
                               else None), \
             mock.patch.object(ron_ai.http_pool, "pool_for",
                               return_value=pool):
            out = ron_ai.probe_providers(timeout=5)
        self.assertEqual(out["pollinations"], (True, "ok (1 models)"))
        self.assertNotIn("Authorization", pool.got_headers)
        self.assertEqual(out["groq"], (False, "no key in vault"))


if __name__ == "__main__":
    unittest.main()
