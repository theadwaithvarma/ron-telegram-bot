#!/usr/bin/env python3
"""Phase 6 tests: rolling history summaries. No network, no Telegram.

Run: python3 tests/test_summary.py  (or python3 -m unittest discover tests)
"""
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.expanduser("~"), "workspace",
                                "telegram-bot"))

from cryptography.fernet import Fernet

import ron_memory
import ron_ai

TEST_KEY = Fernet.generate_key().decode()


def fresh_store():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return ron_memory.MemoryStore(path=tmp.name), tmp.name


class FakeResp:
    """Mimics the context-manager + read() protocol chat() uses."""

    def __init__(self, content):
        self._data = json.dumps(
            {"choices": [{"message": {"content": content}}]}).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ChatMetaTest(unittest.TestCase):
    """MemoryStore.get_meta/set_meta: encrypted per-chat key/value."""

    def setUp(self):
        os.environ[ron_memory.KEY_ENV] = TEST_KEY
        self.store, self.path = fresh_store()

    def tearDown(self):
        self.store.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_round_trip(self):
        self.store.set_meta("A", "summary", "they talked about exams")
        self.assertEqual(self.store.get_meta("A", "summary"),
                         "they talked about exams")

    def test_overwrite(self):
        self.store.set_meta("A", "summary", "v1")
        self.store.set_meta("A", "summary", "v2")
        self.assertEqual(self.store.get_meta("A", "summary"), "v2")

    def test_per_chat_isolation(self):
        self.store.set_meta("A", "summary", "A's stuff")
        self.store.set_meta("B", "summary", "B's stuff")
        self.assertEqual(self.store.get_meta("A", "summary"), "A's stuff")
        self.assertEqual(self.store.get_meta("B", "summary"), "B's stuff")

    def test_missing_returns_none(self):
        self.assertIsNone(self.store.get_meta("A", "nope"))
        self.assertIsNone(self.store.get_meta("NOCHAT", "summary"))

    def test_wipe_chat_deletes_meta(self):
        self.store.set_meta("X", "summary", "wipe me")
        self.store.set_meta("Y", "summary", "keep me")
        out = self.store.wipe_chat("X")
        self.assertGreater(out["meta"], 0)
        self.assertIsNone(self.store.get_meta("X", "summary"))
        self.assertEqual(self.store.get_meta("Y", "summary"), "keep me")

    def test_meta_encrypted_at_rest(self):
        secret = "xyzzzy summary secret"
        self.store.set_meta("A", "summary", secret)
        with open(self.path, "rb") as fh:
            raw = fh.read()
        self.assertNotIn(secret.encode(), raw)
        self.assertEqual(self.store.get_meta("A", "summary"), secret)


class RonAiHistoryBase(unittest.TestCase):
    """Isolate the JSON history file + summarizer hooks per test."""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".hist.json", delete=False)
        tmp.close()
        self.hist_path = tmp.name
        self._orig = (ron_ai.HISTORY_FILE, ron_ai._request,
                      ron_ai.MEMORY_SUMMARY_GET, ron_ai.MEMORY_SUMMARY_SET,
                      ron_ai.MEMORY_HOOK)
        ron_ai.HISTORY_FILE = self.hist_path
        ron_ai.MEMORY_HOOK = None
        self.summaries = {}  # dict-backed summary store
        ron_ai.MEMORY_SUMMARY_GET = lambda cid: self.summaries.get(str(cid))
        ron_ai.MEMORY_SUMMARY_SET = (
            lambda cid, text: self.summaries.__setitem__(str(cid), text))

    def tearDown(self):
        (ron_ai.HISTORY_FILE, ron_ai._request, ron_ai.MEMORY_SUMMARY_GET,
         ron_ai.MEMORY_SUMMARY_SET, ron_ai.MEMORY_HOOK) = self._orig
        try:
            os.unlink(self.hist_path)
        except OSError:
            pass

    def canned_request(self, text):
        def fake(cfg, messages, stream, timeout, cheap=False):
            return FakeResp(text)
        return fake

    def seed_turns(self, chat_id, n_pairs, prefix="m"):
        """Seed n_pairs of user/assistant turns via _save_turn."""
        for i in range(n_pairs):
            ron_ai._save_turn(chat_id, f"{prefix}{i}", "U", f"r{i}")

    def stored_turns(self, chat_id):
        return ron_ai.load_history().get(str(chat_id), [])


class RolloverTest(RonAiHistoryBase):
    def test_rollover_trigger_folds_oldest_half(self):
        ron_ai._request = self.canned_request("CANNED SUMMARY CHUNK")
        self.seed_turns("C1", 21)  # 42 stored turns > 40 -> rollover
        turns, summary = ron_ai._prompt_turns("C1", "new question", "U")
        # summary stored via the hook and returned
        self.assertEqual(self.summaries.get("C1"), "CANNED SUMMARY CHUNK")
        self.assertEqual(summary, "CANNED SUMMARY CHUNK")
        # history trimmed to ~half: keep = 42 - 21 = 21 turns
        self.assertEqual(len(self.stored_turns("C1")), 21)
        # returned window is bounded and carries the new message
        self.assertLessEqual(len(turns), ron_ai.PROMPT_RECENT)
        self.assertEqual(turns[-1]["content"], "[U]: new question")

    def test_no_rollover_at_or_below_threshold(self):
        def boom(*a, **kw):
            raise AssertionError("_request must not be called")
        ron_ai._request = boom
        self.seed_turns("C2", 20)  # exactly 40 stored turns: no rollover
        before = self.stored_turns("C2")
        turns, summary = ron_ai._prompt_turns("C2", "hi", "U")
        self.assertEqual(summary, "")
        self.assertEqual(self.summaries, {})
        self.assertEqual(len(self.stored_turns("C2")), 40)
        self.assertEqual(self.stored_turns("C2"), before)
        self.assertEqual(turns[-1]["content"], "[U]: hi")

    def test_rollover_fallback_on_summarizer_error(self):
        def boom(*a, **kw):
            raise RuntimeError("provider down")
        ron_ai._request = boom
        self.seed_turns("C3", 21)  # 42 > 40, but summarizer dies
        before = self.stored_turns("C3")
        turns, summary = ron_ai._prompt_turns("C3", "hello", "U")
        # silent fallback: old full-history behavior
        self.assertEqual(summary, "")
        self.assertEqual(self.summaries, {})
        self.assertEqual(len(self.stored_turns("C3")), 42)
        self.assertEqual(self.stored_turns("C3"), before)
        self.assertLessEqual(len(turns), ron_ai.PROMPT_RECENT)
        self.assertEqual(turns[-1]["content"], "[U]: hello")

    def test_rollover_appends_to_prior_summary(self):
        self.summaries["C3b"] = "PRIOR"
        ron_ai._request = self.canned_request("CHUNK")
        self.seed_turns("C3b", 21)
        _turns, summary = ron_ai._prompt_turns("C3b", "q", "U")
        self.assertEqual(summary, "PRIOR\nCHUNK")
        self.assertEqual(self.summaries["C3b"], "PRIOR\nCHUNK")

    def test_summary_truncation_keeps_tail(self):
        big = "X" * 1292 + "TAILMARK"  # 1300 chars > SUMMARY_MAX_CHARS
        ron_ai._request = self.canned_request(big)
        self.seed_turns("C6", 21)
        _turns, summary = ron_ai._prompt_turns("C6", "q", "U")
        self.assertEqual(len(summary), ron_ai.SUMMARY_MAX_CHARS)
        self.assertTrue(summary.endswith("TAILMARK"))
        self.assertEqual(self.summaries["C6"], summary)


class SummarizerPromptTest(RonAiHistoryBase):
    def test_summarizer_prompt_mentions_third_person(self):
        captured = {}

        def fake(cfg, messages, stream, timeout, cheap=False):
            captured["messages"] = messages
            return FakeResp("chunk")
        ron_ai._request = fake
        self.seed_turns("C5", 21)
        ron_ai._prompt_turns("C5", "ping", "U")
        sys_msg = captured["messages"][0]["content"].lower()
        self.assertIn("third person", sys_msg)
        self.assertIn("continuity", sys_msg)
        # only this chat's content is folded in
        user_msg = captured["messages"][1]["content"]
        self.assertIn("Older messages to fold in:", user_msg)
        folded = user_msg.split("Older messages to fold in:")[1]
        self.assertIn("user:", folded)
        self.assertIn("assistant:", folded)

    def test_summarize_via_cheap_none_on_empty_reply(self):
        ron_ai._request = self.canned_request("   ")
        chunk = ron_ai._summarize_via_cheap(
            {}, [{"role": "user", "content": "hi"}], "")
        self.assertIsNone(chunk)

    def test_summarize_via_cheap_none_on_exception(self):
        def boom(*a, **kw):
            raise RuntimeError("down")
        ron_ai._request = boom
        chunk = ron_ai._summarize_via_cheap(
            {}, [{"role": "user", "content": "hi"}], "")
        self.assertIsNone(chunk)

    def test_summarize_via_cheap_caps_chunk(self):
        ron_ai._request = self.canned_request("Z" * 3000)
        chunk = ron_ai._summarize_via_cheap(
            {}, [{"role": "user", "content": "hi"}], "")
        self.assertEqual(len(chunk), 1500)


class ChatPromptTest(RonAiHistoryBase):
    def test_chat_injects_summary_and_bounds_turns(self):
        captured = {}

        def fake(cfg, messages, stream, timeout, cheap=False):
            captured["messages"] = messages
            return FakeResp("got it")
        ron_ai._request = fake
        self.summaries["C4"] = "PRIOR SUMMARY: user likes chess."
        self.seed_turns("C4", 15, prefix="old")  # 30 turns <= 40: no rollover
        reply = ron_ai.chat("C4", "new question", user_name="U")
        self.assertEqual(reply, "got it")
        msgs = captured["messages"]
        system = msgs[0]["content"]
        self.assertIn("[Earlier in this chat — summarized for continuity:]",
                      system)
        self.assertIn("PRIOR SUMMARY: user likes chess.", system)
        hist_turns = msgs[1:]
        self.assertLessEqual(len(hist_turns), ron_ai.PROMPT_RECENT)
        self.assertEqual(len(hist_turns), ron_ai.PROMPT_RECENT)
        # oldest dropped, the just-asked message present
        contents = [t["content"] for t in hist_turns]
        self.assertEqual(hist_turns[-1]["content"], "[U]: new question")
        self.assertNotIn("[U]: old0", contents)
        self.assertNotIn("[U]: old7", contents)
        self.assertIn("[U]: old8", contents)

    def test_chat_without_summary_has_no_summary_block(self):
        captured = {}

        def fake(cfg, messages, stream, timeout, cheap=False):
            captured["messages"] = messages
            return FakeResp("ok")
        ron_ai._request = fake
        ron_ai.chat("C7", "what is recursion", user_name="U")
        system = captured["messages"][0]["content"]
        self.assertNotIn("[Earlier in this chat", system)


class ConcurrencyTest(RonAiHistoryBase):
    def test_concurrent_save_turn_no_corruption(self):
        errs = []

        def worker(i):
            try:
                for j in range(10):
                    ron_ai._save_turn("CT", f"u{i}-{j}", "U", f"r{i}-{j}")
            except Exception as e:  # noqa: BLE001 - smoke test capture
                errs.append(e)

        threads = [threading.Thread(target=worker, args=(i,))
                  for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errs, [])
        stored = self.stored_turns("CT")
        # 160 turns written, store trims to HISTORY_STORE_MAX
        self.assertEqual(len(stored), ron_ai.HISTORY_STORE_MAX)
        for t in stored:
            self.assertIn(t["role"], ("user", "assistant"))
            self.assertIn("content", t)


if __name__ == "__main__":
    unittest.main(verbosity=2)
