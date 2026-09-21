#!/usr/bin/env python3
"""Username plumbing regression tests.

Covers the v1.17.0 incident (2026-09-21): adding the real Telegram
@username to the AI prompt broke every reply because ai_reply() in
ron_daemon.py referenced a bare `username` that was only defined in
handle_update()'s scope. Every AI call died with
`NameError: name 'username' is not defined`, the failures looked
systemic, degraded mode kicked in, and the bot went unresponsive.

Guards under test:
  1. ai_reply() accepts a username kwarg and forwards it to the
     streaming path (stream_ai_reply) — no NameError.
  2. ai_reply() forwards it to the non-streaming path (ron_ai.chat).
  3. Omitting username still works (defaults to "").
  4. Static check: every `username` load in ron_daemon.py resolves to a
     bound name in its scope chain (parameters, assignments, or closure
     capture) — the exact failure mode of v1.17.0.

No network, no Telegram. Run: python3 tests/test_username_plumbing.py
"""
import ast
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_daemon


class _SyncThread:
    """threading.Thread replacement that runs the target synchronously."""

    def __init__(self, target=None, daemon=None, **kw):
        self._target = target

    def start(self):
        self._target()


class AiReplyUsernameTest(unittest.TestCase):
    def setUp(self):
        # Silence everything around the AI call; capture what matters.
        self.stream_calls = []
        self.chat_calls = []
        self._orig = {}
        for name in ("_brain_down", "_cache_lookup", "stream_ai_reply",
                     "_record_brain_success", "_cache_store",
                     "typing_kickoff", "send_rich_with_kb",
                     "handle_group_sends", "handle_send_topic",
                     "handle_topic_actions", "_track", "_untrack",
                     "_BAKE_UNTIL"):
            self._orig[name] = getattr(ron_daemon, name)
        ron_daemon._brain_down = lambda: False
        # v1.25.0: neutralize the canary-deploy bake window — these tests
        # are about username plumbing, not bake routing.
        ron_daemon._BAKE_UNTIL = 0
        ron_daemon._cache_lookup = lambda *a, **k: None
        ron_daemon.stream_ai_reply = (
            lambda *a, **k: self.stream_calls.append((a, k)) or "canned")
        ron_daemon._record_brain_success = lambda *a, **k: None
        ron_daemon._cache_store = lambda *a, **k: None
        ron_daemon.typing_kickoff = lambda *a, **k: type(
            "K", (), {"join": lambda self, timeout=None: None})()
        ron_daemon.send_rich_with_kb = lambda *a, **k: None
        ron_daemon.handle_group_sends = lambda *a, **k: None
        ron_daemon.handle_send_topic = lambda *a, **k: None
        ron_daemon.handle_topic_actions = lambda *a, **k: None
        ron_daemon._track = lambda *a, **k: None
        ron_daemon._untrack = lambda *a, **k: None
        self._orig_chat = ron_daemon.ron_ai.chat
        ron_daemon.ron_ai.chat = (
            lambda *a, **k: self.chat_calls.append((a, k)) or "canned")
        self._orig_lm = ron_daemon.ron_ai.light_mode_enabled
        ron_daemon.ron_ai.light_mode_enabled = lambda: False
        self._orig_thread = ron_daemon.threading.Thread
        ron_daemon.threading.Thread = _SyncThread

    def tearDown(self):
        for name, fn in self._orig.items():
            setattr(ron_daemon, name, fn)
        ron_daemon.ron_ai.chat = self._orig_chat
        ron_daemon.ron_ai.light_mode_enabled = self._orig_lm
        ron_daemon.threading.Thread = self._orig_thread

    def test_stream_path_receives_username(self):
        # private=True, no business conn, no light mode -> streaming path
        ron_daemon.ai_reply(1, 2, "Adwaith", "ping me", private=True,
                            username="TheAdwaith")
        self.assertEqual(len(self.stream_calls), 1)
        args, kwargs = self.stream_calls[0]
        self.assertEqual(kwargs.get("username"), "TheAdwaith")

    def test_nonstream_path_receives_username(self):
        # private=False -> non-streaming ron_ai.chat path
        ron_daemon.ai_reply(1, 2, "Adwaith", "ping me", private=False,
                            username="TheAdwaith")
        self.assertEqual(len(self.chat_calls), 1)
        args, kwargs = self.chat_calls[0]
        self.assertEqual(kwargs.get("username"), "TheAdwaith")

    def test_username_defaults_to_empty(self):
        # Old call sites (e.g. degraded replay) omit username entirely.
        ron_daemon.ai_reply(1, 2, "Adwaith", "ping me", private=True)
        self.assertEqual(len(self.stream_calls), 1)
        args, kwargs = self.stream_calls[0]
        self.assertEqual(kwargs.get("username"), "")


class UsernameScopeTest(unittest.TestCase):
    """Static scope check: no unbound `username` loads, closures included."""

    def test_all_username_loads_resolve(self):
        path = os.path.join(os.path.dirname(__file__), "..", "ron_daemon.py")
        tree = ast.parse(open(path).read())
        problems = []

        def bound_in_chain(node, scopes):
            # scopes: innermost-first list of bound-name sets
            for bound in scopes:
                if node.id in bound:
                    return True
            return False

        def visit(node, scopes):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.Lambda)):
                bound = {a.arg for a in node.args.args}
                bound |= {a.arg for a in node.args.kwonlyargs}
                if node.args.vararg:
                    bound.add(node.args.vararg.arg)
                if node.args.kwarg:
                    bound.add(node.args.kwarg.arg)
                scopes = [bound] + scopes
            elif isinstance(node, (ast.comprehension,)):
                pass
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.Name) and isinstance(
                        child.ctx, ast.Store):
                    if scopes:
                        scopes[0].add(child.id)
            for child in ast.iter_child_nodes(node):
                if (isinstance(child, ast.Name)
                        and isinstance(child.ctx, ast.Load)
                        and child.id == "username"
                        and not bound_in_chain(child, scopes)):
                    problems.append(
                        f"line {child.lineno}: unbound `username` load")
            for child in ast.iter_child_nodes(node):
                visit(child, scopes)

        visit(tree, [])
        self.assertEqual(problems, [], "\n".join(problems))


if __name__ == "__main__":
    unittest.main(verbosity=2)
