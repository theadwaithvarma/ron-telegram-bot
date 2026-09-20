#!/usr/bin/env python3
"""Phase 6 tests: per-chat semantic answer cache. No network, no Telegram.

Run: python3 -m unittest discover tests -v   (from ~/workspace/telegram-bot)
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_cache


def fresh_paths(test):
    """Temp DB + stats paths for one test (isolated, auto-cleaned)."""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    return (os.path.join(tmp.name, "cache.db"),
            os.path.join(tmp.name, "stats.json"))


class NormalizeTest(unittest.TestCase):
    def test_variants_identical(self):
        variants = [
            "What is Python?!",
            "what is python",
            "  WHAT   IS   PYTHON  ",
            "what\u2019s python",          # curly apostrophe
            "\uFF57\uFF48\uFF41\uFF54 is python",  # full-width "what"
        ]
        norms = {ron_cache.normalize(v) for v in variants[:3]}
        self.assertEqual(len(norms), 1)
        self.assertEqual(ron_cache.normalize(variants[0]), "what is python")
        # unicode variants fold to the same form
        self.assertEqual(ron_cache.normalize(variants[3]), "what s python")
        self.assertEqual(ron_cache.normalize(variants[4]), "what is python")

    def test_keeps_non_latin_letters(self):
        self.assertEqual(ron_cache.normalize("Namaste, Duniya!"),
                         "namaste duniya")


class SimilarityTest(unittest.TestCase):
    def test_identical_is_one(self):
        self.assertEqual(ron_cache.similarity("What is Python?",
                                             "what is python"), 1.0)

    def test_paraphrase_hits_threshold(self):
        s = ron_cache.similarity("what is python used for", "what is python")
        self.assertGreaterEqual(s, ron_cache.HIT_THRESHOLD)

    def test_unrelated_below_threshold(self):
        s = ron_cache.similarity("what is python", "how do I bake bread")
        self.assertLess(s, ron_cache.HIT_THRESHOLD)

    def test_short_query_sane(self):
        s = ron_cache.similarity("who are you", "who r u")
        self.assertGreaterEqual(s, 0.0)
        self.assertLessEqual(s, 1.0)
        self.assertLess(s, ron_cache.HIT_THRESHOLD)  # not a cache hit

    def test_stopwords_equivalent(self):
        self.assertEqual(
            ron_cache.similarity("what is the python", "what is python"), 1.0)


class IsCacheableTest(unittest.TestCase):
    def test_exclusions(self):
        for text in ["/ping", "ok", "what's the latest news",
                     "price of bitcoin", "what happened today",
                     "meet me on 21/09", "in 2026",
                     "remember this: my exam Monday", "forget that",
                     "forget everything about me", "forget the password",
                     "", "   "]:
            with self.subTest(text=text):
                self.assertFalse(ron_cache.is_cacheable(text),
                                 f"{text!r} should not be cacheable")

    def test_cacheable(self):
        self.assertTrue(ron_cache.is_cacheable("explain recursion simply"))


class LookupStoreTest(unittest.TestCase):
    def setUp(self):
        self.db, self.stats = fresh_paths(self)

    def test_roundtrip(self):
        self.assertTrue(ron_cache.store("A", "explain recursion simply",
                                       "recursion is ...", self.db))
        self.assertEqual(
            ron_cache.lookup("A", "explain recursion simply",
                             self.db, self.stats),
            "recursion is ...")

    def test_fuzzy_hit(self):
        ron_cache.store("A", "what is python used for", "a language",
                        self.db)
        # paraphrase of the stored question -> cache hit
        self.assertEqual(
            ron_cache.lookup("A", "what is python", self.db, self.stats),
            "a language")

    def test_miss(self):
        self.assertIsNone(ron_cache.lookup("A", "explain quantum tunneling",
                                          self.db, self.stats))

    def test_per_chat_isolation(self):
        ron_cache.store("A", "explain recursion simply", "reply A", self.db)
        # same text in another chat -> miss, and A's row untouched
        self.assertIsNone(ron_cache.lookup("B", "explain recursion simply",
                                           self.db, self.stats))
        stats = ron_cache.get_stats(self.stats)
        self.assertEqual(stats["misses"], 1)
        self.assertEqual(stats["hits"], 0)
        self.assertEqual(
            ron_cache.lookup("A", "explain recursion simply",
                             self.db, self.stats),
            "reply A")

    def test_ttl_expiry(self):
        ron_cache.store("A", "explain recursion simply", "reply", self.db)
        old = time.time() - ron_cache.CACHE_TTL_SECS - 100
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE cache SET created_ts = ?, last_hit_ts = ?",
                     (old, old))
        conn.commit()
        conn.close()
        self.assertIsNone(ron_cache.lookup("A", "explain recursion simply",
                                           self.db, self.stats))

    def test_lru_eviction(self):
        old_cap = ron_cache.MAX_ENTRIES_PER_CHAT
        ron_cache.MAX_ENTRIES_PER_CHAT = 5
        self.addCleanup(setattr, ron_cache, "MAX_ENTRIES_PER_CHAT", old_cap)
        qs = ["explain recursion simply",
              "what is a closure in programming",
              "how does garbage collection work",
              "describe the observer pattern",
              "what is big o notation",
              "explain polymorphism briefly"]
        for i, q in enumerate(qs):
            self.assertTrue(ron_cache.store("A", q, f"reply{i}", self.db))
        # oldest (qs[0]) evicted, newest (qs[5]) present
        self.assertIsNone(ron_cache.lookup("A", qs[0], self.db, self.stats))
        self.assertEqual(ron_cache.lookup("A", qs[5], self.db, self.stats),
                         "reply5")
        # recency ordering: bump qs[1] via lookup, add another -> qs[2] goes
        self.assertEqual(ron_cache.lookup("A", qs[1], self.db, self.stats),
                         "reply1")
        self.assertTrue(ron_cache.store("A", "what is dynamic typing",
                                       "reply6", self.db))
        self.assertEqual(ron_cache.lookup("A", qs[1], self.db, self.stats),
                         "reply1")   # bumped, survived
        self.assertIsNone(ron_cache.lookup("A", qs[2], self.db, self.stats))

    def test_stats_accumulate(self):
        q, r = "explain recursion simply", "recursion is a function calling"
        ron_cache.store("A", q, r, self.db)
        ron_cache.lookup("A", q, self.db, self.stats)          # hit
        ron_cache.lookup("A", "explain quantum tunneling",
                         self.db, self.stats)                  # miss
        stats = ron_cache.get_stats(self.stats)
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 1)
        self.assertEqual(stats["tokens_saved_est"],
                         ron_cache.est_tokens(q, r))

    def test_noncacheable_lookup_touches_nothing(self):
        self.assertIsNone(ron_cache.lookup("A", "/ping", self.db, self.stats))
        self.assertIsNone(ron_cache.lookup("A", "ok", self.db, self.stats))
        self.assertEqual(ron_cache.get_stats(self.stats),
                         {"hits": 0, "misses": 0, "tokens_saved_est": 0})
        self.assertFalse(os.path.exists(self.stats))

    def test_store_refusals(self):
        self.assertFalse(ron_cache.store("A", "/ping", "reply", self.db))
        self.assertFalse(ron_cache.store("A", "ok", "reply", self.db))
        self.assertFalse(ron_cache.store("A", "explain recursion simply",
                                        "", self.db))
        self.assertFalse(ron_cache.store("A", "explain recursion simply",
                                        "   ", self.db))
        self.assertFalse(os.path.exists(self.db))  # nothing written

    def test_update_path(self):
        ron_cache.store("A", "explain recursion simply", "old reply",
                        self.db)
        ron_cache.store("A", "Explain Recursion Simply!", "new reply",
                        self.db)   # same normalized question
        self.assertEqual(
            ron_cache.lookup("A", "explain recursion simply",
                             self.db, self.stats),
            "new reply")

    def test_thread_smoke(self):
        errors = []

        def worker(n):
            try:
                for i in range(20):
                    q = f"worker {n} asks about topic number {i} here"
                    ron_cache.store(f"chat{n % 3}", q, f"reply {i}", self.db)
                    ron_cache.lookup(f"chat{n % 3}", q, self.db, self.stats)
            except Exception as e:  # noqa: BLE001 -- collect, don't crash
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
