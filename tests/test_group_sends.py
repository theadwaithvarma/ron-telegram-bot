#!/usr/bin/env python3
"""Tests for [send_group: ...] trailer execution in handle_group_sends.

Covers the v1.20.0 fix (2026-09-21): the model sometimes tacks the
trailer onto the end of a sentence ("Done! [send_group: G | hi]")
instead of its own line. The old code only matched GROUP_RE (own-line
form), so the tail form was silently stripped by strip_trailer_syntax()
without ever firing — the user saw "done!" but nothing posted.

Guards:
  1. Own-line trailer fires (existing behavior, unchanged).
  2. Tail-form trailer (end of a sentence) fires.
  3. A trailer alone on the final line matches BOTH regexes — it must
     fire exactly once (never double-post a ping).
  4. Malformed trailers (no "|", empty parts) fire nothing.
  5. Unknown group -> error goes to the origin chat, no ping.

No network, no Telegram. Run: python3 tests/test_group_sends.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_daemon


class GroupSendsTest(unittest.TestCase):
    def setUp(self):
        self.pings = []
        self.sends = []
        self._orig = {}
        for name in ("resolve_group", "send_group_ping", "send",
                     "load_known_groups"):
            self._orig[name] = getattr(ron_daemon, name)
        ron_daemon.resolve_group = (
            lambda n: (12345, None) if n == "TestGroup"
            else (None, f"I don't see a group called '{n}'"))
        ron_daemon.send_group_ping = (
            lambda gid, uid, uname, msg:
            self.pings.append((gid, uid, uname, msg)) or True)
        ron_daemon.send = (
            lambda chat_id, text, *a, **k: self.sends.append((chat_id, text)))
        ron_daemon.load_known_groups = (
            lambda: {"12345": {"title": "TestGroup"}})

    def tearDown(self):
        for name, fn in self._orig.items():
            setattr(ron_daemon, name, fn)

    def fire(self, reply):
        ron_daemon.handle_group_sends(999, 42, "Adwaith", reply)

    def test_own_line_trailer_fires(self):
        self.fire("Done!\n[send_group: TestGroup | hello there]")
        self.assertEqual(len(self.pings), 1)
        gid, uid, uname, msg = self.pings[0]
        self.assertEqual((gid, uid, uname, msg),
                         (12345, 42, "Adwaith", "hello there"))

    def test_tail_form_trailer_fires(self):
        # The bug: trailer tacked onto a sentence never fired.
        self.fire("Done! [send_group: TestGroup | hello there]")
        self.assertEqual(len(self.pings), 1)
        self.assertEqual(self.pings[0][3], "hello there")

    def test_final_line_trailer_fires_exactly_once(self):
        # Matches BOTH GROUP_RE and GROUP_TAIL_RE — must not double-post.
        self.fire("Done!\n[send_group: TestGroup | hello there]")
        self.assertEqual(len(self.pings), 1)

    def test_final_line_trailer_with_trailing_newline_fires_once(self):
        self.fire("Done!\n[send_group: TestGroup | hello there]\n")
        self.assertEqual(len(self.pings), 1)

    def test_no_pipe_fires_nothing(self):
        self.fire("Done! [send_group: TestGroup]")
        self.assertEqual(self.pings, [])
        self.assertEqual(self.sends, [])

    def test_unknown_group_errors_to_origin(self):
        self.fire("[send_group: Nope | hi]")
        self.assertEqual(self.pings, [])
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(self.sends[0][0], 999)
        self.assertIn("Nope", self.sends[0][1])

    def test_strip_removes_both_forms(self):
        # strip_trailer_syntax already removed both forms; the ping must
        # have fired anyway (no silent failure).
        reply = "Done! [send_group: TestGroup | hi]"
        self.fire(reply)
        clean = ron_daemon.strip_trailer_syntax(reply)
        self.assertNotIn("send_group", clean)
        self.assertEqual(len(self.pings), 1)


if __name__ == "__main__":
    unittest.main()
