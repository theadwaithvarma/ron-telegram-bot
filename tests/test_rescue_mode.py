#!/usr/bin/env python3
"""Rescue mode ("ghost mode") tests (v1.24.4).

When every AI provider is down (_brain_down), the bot is dying but not
dead: the owner's messages still get an instant low-energy vitals pulse
(zero AI calls), while everyone else keeps the queue + degraded notice.

No network, no Telegram. Run: python3 tests/test_rescue_mode.py
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_daemon

OWNER_ID = ron_daemon.OWNER_ID


class RescuePulseTest(unittest.TestCase):
    def setUp(self):
        ron_daemon._rescue_pulse_ts.clear()
        self.sent = []
        p = mock.patch.object(ron_daemon, "send",
                              lambda c, t, *a, **k: self.sent.append((c, t)))
        p.start()
        self.addCleanup(p.stop)

    def test_pulse_contains_vitals(self):
        ron_daemon._send_rescue_pulse(123)
        self.assertEqual(len(self.sent), 1)
        chat_id, text = self.sent[0]
        self.assertEqual(chat_id, 123)
        self.assertIn("rescue mode", text)
        self.assertIn("uptime", text)
        self.assertIn("queued", text)
        self.assertIn("restarts", text)

    def test_pulse_names_no_bot_identity(self):
        # low-energy: plain vitals, never claims to be a person
        ron_daemon._send_rescue_pulse(123)
        text = self.sent[0][1]
        self.assertNotIn("I am", text)

    def test_pulse_throttled_per_chat(self):
        ron_daemon._send_rescue_pulse(123)
        ron_daemon._send_rescue_pulse(123)  # within cooldown: silent
        self.assertEqual(len(self.sent), 1)
        ron_daemon._send_rescue_pulse(456)  # other chat: goes through
        self.assertEqual(len(self.sent), 2)

    def test_pulse_never_raises(self):
        with mock.patch.object(ron_daemon, "_provider_chain_info",
                               side_effect=RuntimeError("boom")):
            ron_daemon._send_rescue_pulse(789)  # must not raise
        self.assertEqual(len(self.sent), 1)


class RescueAiReplyTest(unittest.TestCase):
    """Drive ai_reply with _brain_down forced on; check owner vs stranger."""

    def _run(self, user_id):
        events = {"queued": [], "notices": [], "pulses": []}
        ron_daemon._rescue_pulse_ts.clear()
        patches = [
            mock.patch.object(ron_daemon, "_brain_down", lambda: True),
            mock.patch.object(
                ron_daemon, "_queue_message",
                lambda c, u, n, t, *a, **k: events["queued"].append(u)),
            mock.patch.object(
                ron_daemon, "_maybe_degraded_notice",
                lambda *a, **k: events["notices"].append(True)),
            mock.patch.object(
                ron_daemon, "_send_rescue_pulse",
                lambda c, *a, **k: events["pulses"].append(c)),
            mock.patch.object(ron_daemon.ron_ai, "chat",
                              side_effect=AssertionError("AI must not run")),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        ron_daemon.ai_reply(111, user_id, "Someone", "you alive?",
                            cacheable=False)
        for _ in range(100):
            if events["queued"] or events["pulses"] or events["notices"]:
                break
            time.sleep(0.05)
        return events

    def test_owner_gets_pulse_and_queue(self):
        events = self._run(OWNER_ID)
        self.assertEqual(events["pulses"], [111])
        self.assertEqual(events["queued"], [OWNER_ID])
        self.assertEqual(events["notices"], [])

    def test_stranger_gets_notice_and_queue_no_pulse(self):
        events = self._run(999999)
        self.assertEqual(events["pulses"], [])
        self.assertEqual(events["queued"], [999999])
        self.assertEqual(events["notices"], [True])


if __name__ == "__main__":
    unittest.main()
