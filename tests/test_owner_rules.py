#!/usr/bin/env python3
"""v1.19.0 tests: owner rulebook (ron_owner_rules).

Teach-phrase detection, add/remove/list persistence, de-dupe, caps,
and the prompt-injection block. Uses a temp PATH so the live
owner_rules.json is never touched.
"""
import os
import sys
import tempfile
import unittest

REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, REPO)

# NOTE (AGENTS.md): under `unittest discover`, an alphabetically-earlier
# test module may have imported ron_daemon first, which inserts the LIVE
# dir at sys.path[0] — pinning the LIVE ron_owner_rules in sys.modules.
# Evict it and re-import the repo copy.
sys.modules.pop("ron_owner_rules", None)
sys.path.insert(0, REPO)
import ron_owner_rules as rr
assert "ron-telegram-bot" in rr.__file__, rr.__file__


class TestDetectTeach(unittest.TestCase):
    def test_remember_colon(self):
        self.assertEqual(rr.detect_teach("remember: always greet warmly"),
                         "always greet warmly")

    def test_remember_that(self):
        self.assertEqual(rr.detect_teach("remember that code goes in fences"),
                         "code goes in fences")

    def test_remember_to(self):
        self.assertEqual(rr.detect_teach("Remember to keep replies short"),
                         "keep replies short")

    def test_from_now_on(self):
        self.assertEqual(rr.detect_teach("from now on, no buttons on news"),
                         "no buttons on news")

    def test_new_rule(self):
        self.assertEqual(rr.detect_teach("new rule: sign off with Ron"),
                         "sign off with Ron")

    def test_note(self):
        self.assertEqual(rr.detect_teach("note: prefer tables"),
                         "prefer tables")

    def test_remember_this_is_memory_not_teach(self):
        # Per-chat memory ("remember this ...") must NOT become a rule.
        self.assertIsNone(rr.detect_teach("remember this: I like dosa"))

    def test_casual_mention_not_teach(self):
        self.assertIsNone(rr.detect_teach("do you remember yesterday?"))
        self.assertIsNone(rr.detect_teach("I will remember to call"))
        self.assertIsNone(rr.detect_teach(""))

    def test_empty_rule_rejected(self):
        self.assertIsNone(rr.detect_teach("remember:"))

    def test_whitespace_collapsed(self):
        r = rr.detect_teach("remember:  keep\nit   tidy")
        self.assertEqual(r, "keep it tidy")


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        self.tmp.close()
        self._old = rr.PATH
        rr.PATH = self.tmp.name
        # Start from a missing file.
        os.unlink(self.tmp.name)

    def tearDown(self):
        rr.PATH = self._old
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)

    def test_add_and_load(self):
        rule, err = rr.add_rule("always greet warmly")
        self.assertIsNone(err)
        self.assertEqual(rule["text"], "always greet warmly")
        rules = rr.load_rules()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["text"], "always greet warmly")

    def test_dedupe(self):
        rr.add_rule("keep replies short")
        _, err = rr.add_rule("Keep Replies Short")
        self.assertIsNotNone(err)
        self.assertEqual(len(rr.load_rules()), 1)

    def test_remove(self):
        rr.add_rule("rule one")
        rr.add_rule("rule two")
        self.assertEqual(rr.remove_rule(1), "rule one")
        self.assertEqual([r["text"] for r in rr.load_rules()], ["rule two"])
        self.assertIsNone(rr.remove_rule(5))
        self.assertIsNone(rr.remove_rule(0))

    def test_cap(self):
        old_max = rr.MAX_RULES
        rr.MAX_RULES = 2
        try:
            rr.add_rule("a")
            rr.add_rule("b")
            _, err = rr.add_rule("c")
            self.assertIsNotNone(err)
        finally:
            rr.MAX_RULES = old_max

    def test_rules_block(self):
        self.assertEqual(rr.rules_block(), "")
        rr.add_rule("always greet warmly")
        rr.add_rule("no buttons on news")
        blk = rr.rules_block()
        self.assertIn("[Owner's standing rules", blk)
        self.assertIn("1. always greet warmly", blk)
        self.assertIn("2. no buttons on news", blk)

    def test_rules_block_cached_by_mtime(self):
        # rules_block() runs on every AI turn: it must not re-read the
        # file when the mtime hasn't changed, and must pick up edits.
        rr.add_rule("cache me")
        first = rr.rules_block()
        self.assertIn("cache me", first)
        mtime_before = os.path.getmtime(rr.PATH)
        # Rewrite identical content with a forced newer mtime.
        with open(rr.PATH, "r", encoding="utf-8") as f:
            content = f.read()
        new_mtime = mtime_before + 5
        with open(rr.PATH, "w", encoding="utf-8") as f:
            f.write(content)
        os.utime(rr.PATH, (new_mtime, new_mtime))
        # Patch load_rules to count disk reads.
        reads = []
        orig_load = rr.load_rules
        try:
            rr.load_rules = lambda: reads.append(1) or orig_load()
            # mtime changed -> one re-read, same content.
            self.assertEqual(rr.rules_block(), first)
            self.assertEqual(len(reads), 1)
            # mtime unchanged -> served from cache, no disk read.
            self.assertEqual(rr.rules_block(), first)
            self.assertEqual(len(reads), 1)
        finally:
            rr.load_rules = orig_load
        # A real edit invalidates the cache.
        rr.add_rule("second rule")
        self.assertIn("second rule", rr.rules_block())


if __name__ == "__main__":
    unittest.main(verbosity=2)
