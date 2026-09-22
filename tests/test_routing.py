#!/usr/bin/env python3
"""Offline tests for ron_routing (Track 3: difficulty-based routing).

Covers:
  1. classify_difficulty() — clear-cut cheap/standard/hard cases plus
     edge cases (greeting + real question, borderline lengths,
     "explain" without why/how, bare acks).
  2. routing_tier() — light_mode forces cheap (parity with
     ron_ai._use_cheap semantics).
  3. order_provider_models() / build_tiered_plan() — the plan builder:
     hard puts the strongest model first, cheap puts the smallest
     first, standard is unchanged, and no (provider, model) pair is
     ever dropped (failover still walks the whole chain).
  4. Failover-walk simulation over build_tiered_plan() output:
     every entry 429s except the last one — proves the walk visits
     every pair in tier order, i.e. reordering never short-circuits
     the chain.

No network, no Telegram, no ron_daemon state touched.
Run: python3 tests/test_routing.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_routing
from ron_routing import (classify_difficulty, routing_tier,
                         order_provider_models, build_tiered_plan,
                         use_short_prompt)

# Synthetic roster shaped like ron_ai's cfg["providers"], using the
# real model names from _CHEAP_MODELS / _STRONGEST_MODELS.
PROVIDERS = [
    {"name": "groq",
     "models": ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]},
    {"name": "gemini",
     "models": ["gemini-3.8-flash", "gemini-flash-lite-latest"]},
    {"name": "openrouter",
     "models": ["nex-agi/nex-n2.5-pro:free", "openai/gpt-oss-120b:free"]},
]


class ClassifyCheapTest(unittest.TestCase):
    def test_greetings(self):
        for text in ["hi", "hello", "hey", "yo", "sup", "howdy",
                     "namaste", "vanakkam", "Hi!", "HELLO",
                     "how are you", "how r u", "what's up", "whats up",
                     "who are you", "good morning", "good evening",
                     "good night"]:
            with self.subTest(text=text):
                self.assertEqual(classify_difficulty(text), "cheap")

    def test_thanks_and_acks(self):
        # Zero-cost canned-ack territory — still labeled cheap for
        # wiring consistency.
        for text in ["thanks", "thank you", "thx!", "ty", "ok", "okay",
                     "k", "lol", "👍", "nice", "cool"]:
            with self.subTest(text=text):
                self.assertEqual(classify_difficulty(text), "cheap")

    def test_greeting_plus_question_is_not_cheap(self):
        # Anchored whole-message: a greeting that leads into a real
        # question must not be routed cheap.
        self.assertEqual(
            classify_difficulty("hey, what's 2+2?"), "hard")  # math
        self.assertEqual(
            classify_difficulty("hi, can you explain why the sky is blue?"),
            "hard")  # why-explanation
        self.assertEqual(
            classify_difficulty("hello there, what can you do?"),
            "standard")  # not a bare greeting

    def test_long_greeting_shaped_text_not_cheap(self):
        # >80 chars never cheap (parity with _looks_trivial's cap).
        self.assertNotEqual(
            classify_difficulty("hi " * 30), "cheap")

    def test_empty_is_standard(self):
        self.assertEqual(classify_difficulty(""), "standard")
        self.assertEqual(classify_difficulty("   "), "standard")

    def test_consistent_with_ron_ai_looks_trivial(self):
        # Every greeting shape ron_ai._looks_trivial treats as cheap
        # (anchored _TRIVIAL_RE alternations, <=80 chars) must also
        # classify cheap here — routing's cheap set is a superset
        # (it additionally covers "good night" and bare acks/thanks).
        trivial_shapes = ["hi", "hello", "hey", "yo", "sup", "howdy",
                          "namaste", "vanakkam", "how are you", "how r u",
                          "hows it going", "how's it going", "whats up",
                          "what's up", "who are you", "who r u",
                          "good morning", "good afternoon", "good evening"]
        for base in trivial_shapes:
            for variant in [base, base + "!", base + "?",
                            base.upper(), "  " + base + "  "]:
                with self.subTest(text=variant):
                    self.assertEqual(classify_difficulty(variant), "cheap")


class ClassifyHardTest(unittest.TestCase):
    def test_code_fences(self):
        self.assertEqual(
            classify_difficulty("```python\ndef f():\n    pass\n```"),
            "hard")
        self.assertEqual(
            classify_difficulty("here's the snippet:\n```\nx = 1\n```"),
            "hard")

    def test_stack_traces(self):
        py_trace = ("Traceback (most recent call last):\n"
                    '  File "bot.py", line 42, in <module>\n'
                    "    main()\n"
                    "TypeError: bad operand")
        self.assertEqual(classify_difficulty(py_trace), "hard")
        java_trace = ("Exception in thread \"main\" "
                      "java.lang.NullPointerException\n"
                      "\tat com.foo.Bar.run(Bar.java:12)")
        self.assertEqual(classify_difficulty(java_trace), "hard")
        self.assertEqual(
            classify_difficulty("getting a KeyError: 'chat_id' here"),
            "hard")

    def test_math(self):
        for text in ["what is 7*8?", "solve 12x + 4 = 28",
                     "calculate 100/4 + 2", "derivative of x^2",
                     "simplify (3+5)*2", "what's 15% of 200"]:
            with self.subTest(text=text):
                self.assertEqual(classify_difficulty(text), "hard")

    def test_explain_why_how(self):
        for text in ["explain why the sky is blue",
                     "explain how photosynthesis works",
                     "why does ice float on water",
                     "how do neural networks learn"]:
            with self.subTest(text=text):
                self.assertEqual(classify_difficulty(text), "hard")

    def test_debugging(self):
        for text in ["why isn't my loop working?",
                     "my code is not working, fix this",
                     "what's wrong with my recursion?",
                     "debug this for me: it throws an error"]:
            with self.subTest(text=text):
                self.assertEqual(classify_difficulty(text), "hard")

    def test_multipart(self):
        self.assertEqual(
            classify_difficulty(
                "Which is better? What are the pros? Which is cheaper?"),
            "hard")  # 3 questions
        self.assertEqual(
            classify_difficulty("1. install it\n2. configure it\n"
                                "3. deploy it"),
            "hard")  # numbered list

    def test_long_complex(self):
        self.assertEqual(classify_difficulty("lorem ipsum " * 60), "hard")
        many_sentences = ("First this happened. Then that happened. "
                          "After that, something else. It kept going. "
                          "Finally it stopped.")
        self.assertEqual(classify_difficulty(many_sentences), "hard")

    def test_howto_tasks(self):
        self.assertEqual(
            classify_difficulty("walk me through setting up a venv"),
            "hard")
        self.assertEqual(
            classify_difficulty("pros and cons of sqlite vs postgres"),
            "hard")


class ClassifyStandardTest(unittest.TestCase):
    def test_everyday_questions(self):
        for text in ["what's the weather today?",
                     "tell me a joke",
                     "who won the match last night",
                     "recommend a good sci-fi movie",
                     "thank you so much for the help"]:
            with self.subTest(text=text):
                self.assertEqual(classify_difficulty(text), "standard")

    def test_explain_without_why_how_stays_standard(self):
        # "explain X" with no why/how mechanism ask is a normal ask.
        self.assertEqual(
            classify_difficulty("explain quantum computing briefly"),
            "standard")

    def test_single_short_question_stays_standard(self):
        self.assertEqual(classify_difficulty("where is the nearest atm?"),
                         "standard")

    def test_numbers_without_operators_stay_standard(self):
        # Digits alone aren't math.
        self.assertEqual(
            classify_difficulty("call me at 5 pm about the 2 tickets"),
            "standard")

    def test_never_raises_on_weird_input(self):
        for text in [None, "", " ", "😂" * 200, "\n\t"]:
            with self.subTest(text=repr(text)[:20]):
                self.assertIn(classify_difficulty(text),
                              ("cheap", "standard", "hard"))


class RoutingTierTest(unittest.TestCase):
    def test_light_mode_forces_cheap(self):
        # Parity with ron_ai._use_cheap: light mode = running lean,
        # every message cheap regardless of classification.
        cfg = {"light_mode": True}
        self.assertEqual(
            routing_tier("explain why the sky is blue", cfg), "cheap")
        self.assertEqual(routing_tier("hi", cfg), "cheap")

    def test_no_cfg_is_pure_classification(self):
        self.assertEqual(routing_tier("hi", {}), "cheap")
        self.assertEqual(routing_tier("hi", None), "cheap")
        self.assertEqual(
            routing_tier("explain why the sky is blue", {}), "hard")

    def test_use_short_prompt(self):
        self.assertTrue(use_short_prompt("cheap"))
        self.assertFalse(use_short_prompt("standard"))
        self.assertFalse(use_short_prompt("hard"))


class OrderModelsTest(unittest.TestCase):
    def test_hard_puts_strongest_first(self):
        self.assertEqual(
            order_provider_models(
                "groq", ["openai/gpt-oss-120b", "openai/gpt-oss-20b"],
                "hard"),
            ["openai/gpt-oss-120b", "openai/gpt-oss-20b"])
        # Strongest not already first: it gets promoted.
        self.assertEqual(
            order_provider_models(
                "gemini",
                ["gemini-flash-lite-latest", "gemini-3.8-flash"],
                "hard"),
            ["gemini-3.8-flash", "gemini-flash-lite-latest"])

    def test_cheap_puts_smallest_first(self):
        # Identical to ron_ai._request's existing cheap reorder.
        self.assertEqual(
            order_provider_models(
                "groq", ["openai/gpt-oss-120b", "openai/gpt-oss-20b"],
                "cheap"),
            ["openai/gpt-oss-20b", "openai/gpt-oss-120b"])
        self.assertEqual(
            order_provider_models(
                "openrouter",
                ["openai/gpt-oss-120b:free", "nex-agi/nex-n2.5-pro:free"],
                "cheap"),
            ["nex-agi/nex-n2.5-pro:free", "openai/gpt-oss-120b:free"])

    def test_standard_unchanged(self):
        models = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
        self.assertEqual(
            order_provider_models("groq", models, "standard"), models)

    def test_unknown_provider_or_model_unchanged(self):
        models = ["m1", "m2"]
        self.assertEqual(
            order_provider_models("nope", models, "hard"), models)
        self.assertEqual(
            order_provider_models("groq", models, "cheap"), models)

    def test_never_drops_models(self):
        for tier in ("cheap", "standard", "hard"):
            for p in PROVIDERS:
                with self.subTest(tier=tier, provider=p["name"]):
                    ordered = order_provider_models(
                        p["name"], p["models"], tier)
                    self.assertEqual(sorted(ordered),
                                     sorted(p["models"]))
                    self.assertEqual(len(ordered), len(p["models"]))


class TieredPlanTest(unittest.TestCase):
    def test_hard_plan_starts_with_groq_120b(self):
        plan = build_tiered_plan(PROVIDERS, "hard")
        self.assertEqual(plan[0], ("groq", "openai/gpt-oss-120b"))
        # Every provider's strongest model leads its own block.
        groq_block = [m for n, m in plan if n == "groq"]
        gemini_block = [m for n, m in plan if n == "gemini"]
        or_block = [m for n, m in plan if n == "openrouter"]
        self.assertEqual(groq_block[0], "openai/gpt-oss-120b")
        self.assertEqual(gemini_block[0], "gemini-3.8-flash")
        self.assertEqual(or_block[0], "openai/gpt-oss-120b:free")

    def test_cheap_plan_starts_with_small_models(self):
        plan = build_tiered_plan(PROVIDERS, "cheap")
        self.assertEqual(plan[0], ("groq", "openai/gpt-oss-20b"))
        gemini_block = [m for n, m in plan if n == "gemini"]
        self.assertEqual(gemini_block[0], "gemini-flash-lite-latest")

    def test_standard_plan_is_default_order(self):
        plan = build_tiered_plan(PROVIDERS, "standard")
        self.assertEqual(plan[0], ("groq", "openai/gpt-oss-120b"))
        self.assertEqual(
            plan,
            [("groq", "openai/gpt-oss-120b"),
             ("groq", "openai/gpt-oss-20b"),
             ("gemini", "gemini-3.8-flash"),
             ("gemini", "gemini-flash-lite-latest"),
             ("openrouter", "nex-agi/nex-n2.5-pro:free"),
             ("openrouter", "openai/gpt-oss-120b:free")])

    def test_plan_keeps_full_chain_every_tier(self):
        expected = {(p["name"], m)
                    for p in PROVIDERS for m in p["models"]}
        for tier in ("cheap", "standard", "hard"):
            with self.subTest(tier=tier):
                plan = build_tiered_plan(PROVIDERS, tier)
                self.assertEqual(set(plan), expected)
                self.assertEqual(len(plan), len(expected))

    def test_unknown_tier_raises(self):
        with self.assertRaises(ValueError):
            build_tiered_plan(PROVIDERS, "ultra")

    def test_provider_block_order_preserved(self):
        # Reordering happens WITHIN each provider's block; provider
        # order (groq -> gemini -> openrouter) is untouched on all tiers.
        for tier in ("cheap", "standard", "hard"):
            with self.subTest(tier=tier):
                plan = build_tiered_plan(PROVIDERS, tier)
                self.assertEqual([n for n, _ in plan],
                                 ["groq"] * 2 + ["gemini"] * 2
                                 + ["openrouter"] * 2)

    def test_failover_walk_visits_every_pair(self):
        """Simulate 429-on-everything-but-the-last: the walk must visit
        every (provider, model) in tier order — reordering must never
        short-circuit failover."""
        for tier in ("cheap", "standard", "hard"):
            with self.subTest(tier=tier):
                plan = build_tiered_plan(PROVIDERS, tier)
                visited = []

                def attempt(name, model):
                    visited.append((name, model))
                    # True == success: only the very last pair answers;
                    # everything before it 429s and the walk continues.
                    return (name, model) == plan[-1]

                for name, model in plan:
                    if attempt(name, model):  # success -> stop
                        break
                    # else: 429 -> continue to next pair
                self.assertEqual(visited, plan,
                                 f"tier {tier}: failover walk skipped pairs")

    def test_hard_failover_reaches_second_provider(self):
        """Concrete: groq 120b 429s -> the walk must reach groq 20b,
        then gemini — the strongest-first plan still degrades through
        the whole chain."""
        plan = build_tiered_plan(PROVIDERS, "hard")
        visited = []
        for name, model in plan:
            visited.append((name, model))
            if (name, model) == ("gemini", "gemini-3.8-flash"):
                break  # pretend this one succeeds
        self.assertEqual(
            visited,
            [("groq", "openai/gpt-oss-120b"),
             ("groq", "openai/gpt-oss-20b"),
             ("gemini", "gemini-3.8-flash")])


if __name__ == "__main__":
    unittest.main()
