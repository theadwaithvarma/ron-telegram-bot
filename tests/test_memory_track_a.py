#!/usr/bin/env python3
"""Track A tests: record_turn fact extraction, recall_context compactness,
handle_memory_nl dispatch, retention semantics, the no-log-leak rule, and
a per-chat isolation audit across every read API.

No network, no Telegram. DBs are temp files; the env key is a throwaway.
"""
import contextlib
import io
import os
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import ron_memory
from ron_memory import MemoryStore, extract_fact_statements, handle_memory_nl

TEST_KEY = ron_memory.generate_key()


def fresh_store():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return MemoryStore(path=tmp.name), tmp.name


class TrackARecordTurn(unittest.TestCase):
    def setUp(self):
        os.environ[ron_memory.KEY_ENV] = TEST_KEY
        self.store, self.path = fresh_store()

    def tearDown(self):
        self.store.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_user_turn_extracts_fact(self):
        r = self.store.record_turn("A", 1, "user",
                                   "remember that I love filter coffee")
        self.assertIsNotNone(r["message_id"])
        self.assertEqual(len(r["fact_ids"]), 1)
        self.assertIn("filter coffee",
                      self.store.recall_facts("A", "filter coffee")[0]["text"])

    def test_this_colon_stripped(self):
        r = self.store.record_turn("A", 1, "user",
                                   "remember this: my dog is called Bruno")
        facts = self.store.recall_facts("A", "Bruno")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["text"], "my dog is called Bruno")

    def test_assistant_turn_extracts_nothing(self):
        r = self.store.record_turn("A", 0, "assistant",
                                   "remember that I love filter coffee")
        self.assertEqual(r["fact_ids"], [])
        self.assertEqual(self.store.facts("A"), [])

    def test_plain_turn_extracts_nothing(self):
        r = self.store.record_turn("A", 1, "user", "what time is the exam")
        self.assertEqual(r["fact_ids"], [])
        self.assertIsNotNone(r["message_id"])

    def test_empty_text(self):
        r = self.store.record_turn("A", 1, "user", "   ")
        self.assertIsNone(r["message_id"])
        self.assertEqual(r["fact_ids"], [])

    def test_duplicate_extraction_dedupes(self):
        r1 = self.store.record_turn("A", 1, "user",
                                    "remember that I love filter coffee")
        r2 = self.store.record_turn("A", 1, "user",
                                    "remember that I love filter coffee")
        self.assertEqual(r1["fact_ids"], r2["fact_ids"])
        self.assertEqual(len(self.store.facts("A")), 1)

    def test_extraction_triggers_contradiction_rule(self):
        self.store.record_turn("A", 1, "user",
                               "remember that my exam is on monday")
        self.store.record_turn("A", 1, "user",
                               "remember that my exam is not on monday")
        active = self.store.facts("A")
        self.assertEqual(len(active), 1)
        self.assertIn("not on monday", active[0]["text"])

    def test_extract_fact_statements_pure(self):
        self.assertEqual(extract_fact_statements("please remember that I am "
                                                "allergic to peanuts"),
                         ["I am allergic to peanuts"])
        self.assertEqual(extract_fact_statements("note down my wifi "
                                                 "password is hunter2"),
                         ["my wifi password is hunter2"])
        self.assertEqual(extract_fact_statements("don't forget that the "
                                                 "deadline is friday"),
                         ["the deadline is friday"])
        self.assertEqual(extract_fact_statements("just chatting here"), [])


class TrackARecallContext(unittest.TestCase):
    def setUp(self):
        os.environ[ron_memory.KEY_ENV] = TEST_KEY
        self.store, self.path = fresh_store()
        self.store.remember("A", "Priya loves filter coffee")
        self.store.record_message("A", 1, "user",
                                  "grab filter coffee after lunch")

    def tearDown(self):
        self.store.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_facts_first(self):
        ctx = self.store.recall_context("A", "filter coffee")
        self.assertIn("Relevant memory from this chat", ctx)
        fact_line = ctx.index("Priya loves")
        msg_line = ctx.index("(earlier)")
        self.assertLess(fact_line, msg_line)

    def test_limit_chars_enforced(self):
        self.store.remember("A", "x" * 500 + " filter coffee")
        ctx = self.store.recall_context("A", "filter coffee", limit_chars=100)
        self.assertLessEqual(len(ctx), 100)

    def test_default_bound(self):
        ctx = self.store.recall_context("A", "filter coffee")
        self.assertLessEqual(len(ctx), 700)

    def test_empty_when_no_match(self):
        self.assertEqual(self.store.recall_context("A", "zzzznomatch"), "")
        self.assertEqual(self.store.recall_context("B", "filter coffee"), "")

    def test_limit_zero(self):
        self.assertEqual(self.store.recall_context("A", "filter coffee",
                                                   limit=0), "")


class TrackAHandleNL(unittest.TestCase):
    def setUp(self):
        os.environ[ron_memory.KEY_ENV] = TEST_KEY
        self.store, self.path = fresh_store()

    def tearDown(self):
        self.store.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_remember(self):
        handled, reply = handle_memory_nl(self.store, "A", 1,
                                           "remember this: my dog is Bruno")
        self.assertTrue(handled)
        self.assertIn("remember", reply.lower())
        self.assertEqual(len(self.store.recall_facts("A", "Bruno")), 1)

    def test_forget_latest(self):
        self.store.remember("A", "my dog is called Bruno")
        handled, reply = handle_memory_nl(self.store, "A", 1, "forget that")
        self.assertTrue(handled)
        self.assertIn("Forgotten", reply)
        self.assertEqual(self.store.facts("A"), [])

    def test_forget_query(self):
        self.store.remember("A", "my dog is called Bruno")
        self.store.remember("A", "I love filter coffee")
        handled, _ = handle_memory_nl(self.store, "A", 1,
                                       "forget about Bruno")
        self.assertTrue(handled)
        self.assertEqual(self.store.recall_facts("A", "Bruno"), [])
        self.assertEqual(len(self.store.recall_facts("A", "coffee")), 1)

    def test_forget_everything_wipes_chat(self):
        self.store.remember("A", "my dog is called Bruno")
        self.store.record_message("A", 1, "user", "hello")
        self.store.set_meta("A", "summary", "some summary")
        self.store.remember("B", "other chat fact")
        handled, reply = handle_memory_nl(self.store, "A", 1,
                                           "forget everything")
        self.assertTrue(handled)
        self.assertIn("Wiped", reply)
        self.assertEqual(self.store.facts("A"), [])
        self.assertEqual(self.store.recall("A", "hello"), [])
        self.assertIsNone(self.store.get_meta("A", "summary"))
        # other chat untouched
        self.assertEqual(len(self.store.facts("B")), 1)

    def test_non_command(self):
        self.assertEqual(handle_memory_nl(self.store, "A", 1,
                                           "what time is the exam"),
                         (False, ""))
        self.assertEqual(handle_memory_nl(self.store, "A", 1, ""), (False, ""))


class TrackARetention(unittest.TestCase):
    def setUp(self):
        os.environ[ron_memory.KEY_ENV] = TEST_KEY
        self.store, self.path = fresh_store()

    def tearDown(self):
        self.store.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_messages_expire(self):
        old = time.time() - 40 * 86400
        self.store.record_message("A", 1, "user", "ancient history", ts=old)
        self.store.record_message("A", 1, "user", "fresh news")
        n = self.store.prune_expired(30)
        self.assertIsInstance(n, int)
        self.assertEqual(n, 1)
        self.assertEqual(self.store.recall("A", "ancient history"), [])
        self.assertEqual(len(self.store.recall("A", "fresh news")), 1)

    def test_superseded_facts_expire_active_facts_kept(self):
        f1 = self.store.remember("A", "my exam is on monday")
        f2 = self.store.remember("A", "my exam is not on monday")
        self.assertNotEqual(f1, f2)
        ancient = time.time() - 40 * 86400
        self.store._db.execute(
            "UPDATE facts SET updated_ts=? WHERE id IN (?,?)",
            (ancient, f1, f2))
        self.store._db.commit()
        n = self.store.prune_expired(30)
        # dead lineage (f1) pruned; active fact (f2) kept by default
        self.assertEqual(n, 1)
        remaining = self.store.facts("A")
        self.assertEqual([f["id"] for f in remaining], [f2])

    def test_fact_retention_expires_stale_active_facts(self):
        fid = self.store.remember("A", "my dog is called Bruno")
        ancient = time.time() - 40 * 86400
        self.store._db.execute("UPDATE facts SET updated_ts=? WHERE id=?",
                               (ancient, fid))
        self.store._db.commit()
        n = self.store.prune_expired(30, fact_retention_days=30)
        self.assertEqual(n, 1)
        self.assertEqual(self.store.facts("A"), [])

    def test_fresh_active_facts_survive_fact_retention(self):
        self.store.remember("A", "my dog is called Bruno")
        n = self.store.prune_expired(30, fact_retention_days=30)
        self.assertEqual(n, 0)
        self.assertEqual(len(self.store.facts("A")), 1)


class TrackANoLogLeak(unittest.TestCase):
    """Stored plaintext must never reach stdout/stderr, including errors."""

    def setUp(self):
        os.environ[ron_memory.KEY_ENV] = TEST_KEY
        self.store, self.path = fresh_store()
        self.secret = "quixotic zebra passphrase 42"

    def tearDown(self):
        self.store.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_no_plaintext_in_logs_or_errors(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            self.store.record_message("A", 1, "user", self.secret)
            self.store.remember("A", "fact with " + self.secret)
            self.store.recall("A", "quixotic")
            self.store.recall_all("A", "quixotic")
            self.store.recall_context("A", "quixotic")
            self.store.forget("A", "quixotic")
            self.store.wipe_chat("A")
            self.store.prune_expired(30)
            try:
                self.store._dec(b"RM1" + b"\x00" * 40)
            except Exception as e:
                # error text must not leak content or key material
                self.assertNotIn(self.secret, str(e))
        out = buf.getvalue()
        self.assertNotIn(self.secret, out)
        self.assertNotIn("quixotic", out)


class TrackAIsolationAudit(unittest.TestCase):
    """Every read API, asked from chat B, must see nothing of chat A."""

    def setUp(self):
        os.environ[ron_memory.KEY_ENV] = TEST_KEY
        self.store, self.path = fresh_store()
        s = self.store
        # Chat "1" is user 1's private DM (chat_id == str(user_id)).
        s.record_message("1", 1, "user", "zebra alpha chat 1 message")
        self.fid = s.remember("1", "zebra alpha chat 1 fact")
        s.note_member("1", 1, "Alice")
        s.record_consent("1", 1, self.fid)
        s.create_pending("disclosure", "1", 1, fact_id=self.fid)
        s.set_meta("1", "summary", "zebra alpha summary")

    def tearDown(self):
        self.store.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_read_apis_isolated(self):
        s = self.store
        self.assertEqual(s.recall("B", "zebra alpha"), [])
        self.assertEqual(s.recall_ranked("B", "zebra alpha"), [])
        self.assertEqual(s.recall_facts("B", "zebra alpha"), [])
        self.assertEqual(s.recall_all("B", "zebra alpha"), [])
        self.assertEqual(s.facts("B"), [])
        self.assertEqual(s.recall_context("B", "zebra alpha"), "")
        self.assertIsNone(s.get_fact("B", self.fid))
        self.assertEqual(s.supersession_chain("B", self.fid), [])
        self.assertEqual(s.consented_facts("B"), [])
        self.assertEqual(s.member_ids("B"), [])
        self.assertIsNone(s.get_meta("B", "summary"))

    def test_sanctioned_cross_scope_paths_still_work(self):
        s = self.store
        # dm_facts: consent-gated identity matching, never merged into recall
        self.assertEqual(len(s.dm_facts(1)), 1)
        self.assertEqual(len(s.consented_facts("1")), 1)

    def test_writes_from_b_leave_a_intact(self):
        s = self.store
        s.wipe_chat("B")
        s.forget("B", "zebra alpha")
        s.forget_latest_fact("B")
        self.assertEqual(len(s.recall("1", "zebra alpha")), 1)
        self.assertEqual(len(s.recall_facts("1", "zebra alpha")), 1)
        self.assertIsNotNone(s.get_meta("1", "summary"))


class TrackACipherDefault(unittest.TestCase):
    def test_stdlib_when_no_cryptography(self):
        os.environ[ron_memory.KEY_ENV] = TEST_KEY
        store, path = fresh_store()
        try:
            if ron_memory.has_cryptography():
                self.assertEqual(store.cipher_kind, "fernet")
            else:
                self.assertEqual(store.cipher_kind, "stdlib")
        finally:
            store.close()
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
