#!/usr/bin/env python3
"""Tests for the typing/draft kickoff: Telegram-side signals must not
delay the AI call (the long pole).

No network, no Telegram.

Run: python3 -m unittest discover tests -v   (from ~/workspace/telegram-bot)
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_daemon


class KickoffTest(unittest.TestCase):
    def test_typing_kickoff_calls_send_typing(self):
        with mock.patch.object(ron_daemon, "send_typing") as st:
            t = ron_daemon.typing_kickoff(456, "biz1")
            t.join(timeout=10)
        st.assert_called_once_with(456, "biz1")
        self.assertFalse(t.is_alive())

    def test_stream_starts_ai_without_waiting_for_telegram(self):
        """produce() must start while send_typing is still in flight."""
        typing_done = threading.Event()
        produce_started = []
        with mock.patch.object(ron_daemon, "send_typing") as st, \
                mock.patch.object(ron_daemon, "draft_update",
                                  return_value=(True, 0)), \
                mock.patch.object(ron_daemon, "send_rich_with_kb"):
            def slow_typing(chat_id):
                time.sleep(2)  # simulate a slow Telegram round-trip
                typing_done.set()
            st.side_effect = slow_typing

            def produce(on_token, stop_event):
                produce_started.append(time.monotonic())
                return "hi"

            t0 = time.monotonic()
            reply = ron_daemon._stream_with_draft(123, produce)

        self.assertEqual(reply, "hi")
        # Old serial code would start produce() only after the 2s typing.
        self.assertLess(produce_started[0] - t0, 1.0)
        self.assertTrue(typing_done.is_set())  # kickoff still completed

    def test_stream_joins_kickoff_before_final_send(self):
        """The final rich send must not race the draft setup."""
        send_order = []
        with mock.patch.object(ron_daemon, "send_typing") as st, \
                mock.patch.object(ron_daemon, "draft_update") as du, \
                mock.patch.object(ron_daemon, "send_rich_with_kb") as sr:
            def slow_draft(chat_id, draft_id, text):
                time.sleep(1)
                send_order.append("draft")
                return (True, 0)
            st.return_value = None
            du.side_effect = slow_draft
            sr.side_effect = lambda *a, **k: send_order.append("final")

            ron_daemon._stream_with_draft(123, lambda o, s: "hi")

        self.assertEqual(send_order, ["draft", "final"])


if __name__ == "__main__":
    unittest.main()
