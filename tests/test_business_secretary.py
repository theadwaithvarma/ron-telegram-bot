#!/usr/bin/env python3
"""Secretary instruction tests (v1.24.3).

Business (Chat Automation) replies go out from Adwaith's personal account
— his name and avatar, not the bot — so ai_reply must inject
BUSINESS_SECRETARY_INSTRUCTION into the system prompt for business chats,
and ONLY for business chats.

No network, no Telegram. Run: python3 tests/test_business_secretary.py
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_ai
import ron_daemon


class SecretaryInstructionContentTest(unittest.TestCase):
    def test_constant_exists_and_names_the_mode(self):
        instr = ron_ai.BUSINESS_SECRETARY_INSTRUCTION
        self.assertIn("SECRETARY MODE", instr)
        self.assertIn("Adwaith", instr)
        self.assertIn("personal assistant", instr)

    def test_forbids_bot_identity_claims(self):
        instr = ron_ai.BUSINESS_SECRETARY_INSTRUCTION
        self.assertIn("Ron", instr)  # named so the model knows what NOT to claim
        self.assertIn("Never claim to be", instr)
        self.assertIn("bot", instr.lower())

    def test_forbids_internals_and_inventions(self):
        instr = ron_ai.BUSINESS_SECRETARY_INSTRUCTION
        self.assertIn("API keys", instr)
        self.assertIn("Never", instr)
        # must not leak provider names into the prompt
        for provider in ("Groq", "Gemini", "OpenRouter"):
            self.assertNotIn(provider, instr)


class SecretaryInjectionTest(unittest.TestCase):
    """Drive ai_reply end-to-end (threads, mocked Telegram) and inspect the
    system_extra that reaches ron_ai.chat."""

    def _run_ai_reply(self, **kwargs):
        captured = {}

        def fake_chat(chat_id, text, user_name, system_extra="",
                      user_id=0, is_private=True, username=""):
            captured["system_extra"] = system_extra
            return "ok"

        class DummyKick:
            def join(self, timeout=None):
                pass

        # v1.25.0: neutralize the bake window — this test is about the
        # secretary instruction, not canary deploys.
        orig_bake = ron_daemon._BAKE_UNTIL
        ron_daemon._BAKE_UNTIL = 0
        self.addCleanup(lambda: setattr(ron_daemon, "_BAKE_UNTIL", orig_bake))

        patches = [
            mock.patch.object(ron_daemon.ron_ai, "chat", fake_chat),
            mock.patch.object(ron_daemon.ron_ai, "light_mode_enabled",
                              lambda: False),
            mock.patch.object(ron_daemon, "group_context", lambda: ""),
            mock.patch.object(ron_daemon, "typing_kickoff",
                              lambda *a, **k: DummyKick()),
            mock.patch.object(ron_daemon, "send_rich_with_kb",
                              lambda *a, **k: None),
            mock.patch.object(ron_daemon, "handle_group_sends",
                              lambda *a, **k: None),
            mock.patch.object(ron_daemon, "handle_send_topic",
                              lambda *a, **k: None),
            mock.patch.object(ron_daemon, "handle_topic_actions",
                              lambda *a, **k: None),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        ron_daemon.ai_reply(111, 222, "Customer", "hello there",
                            cacheable=False, **kwargs)
        for _ in range(100):
            if "system_extra" in captured:
                break
            time.sleep(0.05)
        self.assertIn("system_extra", captured,
                      "ron_ai.chat was never called")
        return captured["system_extra"]

    def test_business_reply_gets_secretary_instruction(self):
        extra = self._run_ai_reply(private=True,
                                   business_connection_id="conn_abc123")
        self.assertIn("SECRETARY MODE", extra)
        self.assertIn(ron_ai.BUSINESS_SECRETARY_INSTRUCTION, extra)

    def test_group_reply_gets_no_secretary_instruction(self):
        extra = self._run_ai_reply(private=False,
                                   business_connection_id=None)
        self.assertNotIn("SECRETARY MODE", extra)


if __name__ == "__main__":
    unittest.main()
