#!/usr/bin/env python3
"""Integration tests for the v1.28.0 upgrade wiring (no network).

Covers the integrator's ron_ai.py changes:
- _route_tier() maps text -> cheap/standard/hard via ron_routing,
  falling back to _use_cheap semantics when ron_routing is missing.
- _request() accepts tier= and still fails over across providers.
- _maybe_tools() is a no-op for cheap turns / missing ron_tools, and
  appends a tool-results block when the model asks for tool help.

Run: python3 tests/test_upgrade_wiring.py
"""
import inspect
import os
import sys
import unittest

REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, REPO)
sys.modules.pop("ron_ai", None)
sys.modules.pop("ron_routing", None)
sys.modules.pop("ron_tools", None)
sys.path.insert(0, REPO)
import ron_ai
import ron_routing

# ron_daemon (imported by earlier test modules under discover) holds a
# reference to the pre-pop ron_ai module object; rewire it so later
# tests that patch ron_ai hit the same object the daemon calls through
# (same pattern as test_react_target.py's ron_media_trailers rewire).
if "ron_daemon" in sys.modules:
    sys.modules["ron_daemon"].ron_ai = ron_ai


class RouteTierTest(unittest.TestCase):
    def test_cheap_smalltalk(self):
        self.assertEqual(ron_ai._route_tier("hello", {}), "cheap")
        self.assertEqual(ron_ai._route_tier("thanks!", {}), "cheap")

    def test_hard_question(self):
        self.assertEqual(
            ron_ai._route_tier(
                "explain why quicksort is O(n log n) with a proof", {}),
            "hard")
        self.assertEqual(
            ron_ai._route_tier("```python\nprint('hi')\n``` fix this", {}),
            "hard")

    def test_standard_default(self):
        self.assertEqual(
            ron_ai._route_tier("what's the weather like today?", {}),
            "standard")

    def test_light_mode_forces_cheap(self):
        self.assertEqual(
            ron_ai._route_tier("explain quantum computing", {"light_mode": True}),
            "cheap")

    def test_fallback_without_routing_module(self):
        orig = ron_ai.ron_routing
        ron_ai.ron_routing = None
        try:
            self.assertEqual(ron_ai._route_tier("hello", {}), "cheap")
            self.assertEqual(
                ron_ai._route_tier("explain quantum computing", {}),
                "standard")
        finally:
            ron_ai.ron_routing = orig


class RequestTierSignatureTest(unittest.TestCase):
    def test_request_accepts_tier(self):
        sig = inspect.signature(ron_ai._request)
        self.assertIn("tier", sig.parameters)
        self.assertEqual(sig.parameters["tier"].default, "standard")
        self.assertNotIn("cheap", sig.parameters)

    def test_tiered_plan_never_drops_pairs(self):
        providers = [
            {"name": "groq", "models": ["m-big", "m-small"]},
            {"name": "zzz-unknown", "models": ["x"]},
        ]
        for tier in ("cheap", "standard", "hard"):
            plan = ron_routing.build_tiered_plan(providers, tier)
            names_models = [(n, m) for n, m in plan]
            self.assertIn(("groq", "m-big"), names_models)
            self.assertIn(("groq", "m-small"), names_models)
            self.assertIn(("zzz-unknown", "x"), names_models)


class MaybeToolsTest(unittest.TestCase):
    def test_cheap_turn_skipped(self):
        msgs = [{"role": "user", "content": "hi"}]
        out = ron_ai._maybe_tools(msgs, "hi", "cheap")
        self.assertIs(out, msgs)

    def test_missing_tools_module_noop(self):
        orig = ron_ai.ron_tools
        ron_ai.ron_tools = None
        try:
            msgs = [{"role": "user", "content": "search the web for cats"}]
            out = ron_ai._maybe_tools(msgs, "search the web for cats",
                                      "standard")
            self.assertIs(out, msgs)
        finally:
            ron_ai.ron_tools = orig

    def test_tool_block_appended(self):
        import ron_tools
        orig_run = ron_tools.maybe_run_tools
        ron_tools.maybe_run_tools = (
            lambda messages, fn, **k: "[tools]\nresult block")
        try:
            msgs = [{"role": "user", "content": "weather in Bengaluru?"}]
            out = ron_ai._maybe_tools(msgs, "weather in Bengaluru?",
                                      "standard")
            self.assertEqual(len(out), 2)
            self.assertEqual(out[0], msgs[0])
            self.assertIn("result block", out[1]["content"])
        finally:
            ron_tools.maybe_run_tools = orig_run

    def test_tool_none_unchanged(self):
        import ron_tools
        orig_run = ron_tools.maybe_run_tools
        ron_tools.maybe_run_tools = lambda messages, fn, **k: None
        try:
            msgs = [{"role": "user", "content": "weather in Bengaluru?"}]
            out = ron_ai._maybe_tools(msgs, "weather in Bengaluru?",
                                      "standard")
            self.assertIs(out, msgs)
        finally:
            ron_tools.maybe_run_tools = orig_run


if __name__ == "__main__":
    unittest.main(verbosity=2)
