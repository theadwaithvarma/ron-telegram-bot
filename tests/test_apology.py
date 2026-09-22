#!/usr/bin/env python3
"""Outage apology tests (v1.25.0).

After a degraded episode drains with >=1 answered entry, the owner gets a
DM with [Yes, send]/[No] buttons. Yes -> one plain-text apology per
affected chat (business ones via their business_connection_id). No ->
nothing. At most one offer/hour; nothing without the owner's tap.

No network, no Telegram. Run: python3 tests/test_apology.py
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_daemon

OWNER_ID = ron_daemon.OWNER_ID


def _queue_entry(chat_id, user_id=111, conn_id=None, ts=None):
    return {"qid": f"{ts or time.time():.6f}:{chat_id}:{user_id}",
            "ts": ts or time.time(), "chat_id": chat_id, "user_id": user_id,
            "user_name": "Someone", "text": "hello?", "reply_to": None,
            "thread_id": None, "business_connection_id": conn_id,
            "private": True}


class ApologyOfferTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_q = ron_daemon.DEGRADED_QUEUE
        ron_daemon.DEGRADED_QUEUE = os.path.join(self.tmp.name, "dq.jsonl")
        self.markups = []   # (chat_id, text, reply_markup)
        self.sends = []     # (chat_id, text, kwargs)
        patches = [
            mock.patch.object(
                ron_daemon, "send_markup",
                lambda c, t, kb, *a, **k: self.markups.append((c, t, kb))),
            mock.patch.object(
                ron_daemon, "send",
                lambda c, t, *a, **k: self.sends.append((c, t, k))),
            mock.patch.object(ron_daemon, "send_rich_with_kb",
                              lambda *a, **k: None),
            mock.patch.object(ron_daemon, "send_typing",
                              lambda *a, **k: None),
            mock.patch.object(ron_daemon, "group_context", lambda: ""),
            mock.patch.object(ron_daemon.ron_ai, "chat",
                              lambda *a, **k: "drained reply"),
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
        # Reset apology + degraded state.
        ron_daemon._apology_pending.clear()
        ron_daemon._apology_last_offer = 0
        ron_daemon._LAST_OUTAGE_SINCE = None
        ron_daemon._degraded["active"] = False
        ron_daemon._brain_failures.clear()

    def tearDown(self):
        ron_daemon.DEGRADED_QUEUE = self.orig_q
        ron_daemon._apology_pending.clear()
        ron_daemon._apology_last_offer = 0
        ron_daemon._LAST_OUTAGE_SINCE = None
        ron_daemon._degraded["active"] = False
        ron_daemon._brain_failures.clear()
        self.tmp.cleanup()

    def _seed_queue(self, entries):
        with open(ron_daemon.DEGRADED_QUEUE, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def _start_outage(self, mins_ago=45):
        ron_daemon._degraded["active"] = True
        ron_daemon._degraded["since"] = time.time() - mins_ago * 60

    def test_drain_after_outage_offers_apology(self):
        self._seed_queue([_queue_entry(111), _queue_entry(222, conn_id="bc1")])
        self._start_outage()
        self.assertTrue(ron_daemon._drain_queue())
        self.assertEqual(len(self.markups), 1)
        chat_id, text, kb = self.markups[0]
        self.assertEqual(chat_id, OWNER_ID)
        self.assertIn("2 chat(s)", text)
        self.assertIn("45 min", text)
        rows = kb["inline_keyboard"]
        datas = [b["callback_data"] for row in rows for b in row]
        self.assertTrue(any(d.startswith("apology:yes:") for d in datas))
        self.assertTrue(any(d.startswith("apology:no:") for d in datas))
        for d in datas:
            self.assertLessEqual(len(d.encode()), 64)
        # Pending episode recorded with both chats.
        self.assertEqual(len(ron_daemon._apology_pending), 1)
        ep = next(iter(ron_daemon._apology_pending))
        pending = ron_daemon._apology_pending[ep]
        self.assertIn((111, None), pending)
        self.assertIn((222, "bc1"), pending)

    def test_failed_offer_does_not_consume_cooldown(self):
        # If send_markup raises, the cooldown must NOT update — the next
        # episode's offer isn't eaten by a failed send.
        self._start_outage(mins_ago=30)
        ron_daemon._degraded["active"] = False
        ron_daemon._LAST_OUTAGE_SINCE = time.time() - 30 * 60
        with mock.patch.object(ron_daemon, "send_markup",
                               side_effect=RuntimeError("net down")):
            ron_daemon._maybe_offer_apology([(123, None)])
        self.assertEqual(ron_daemon._apology_last_offer, 0)
        # A later episode can still offer.
        ron_daemon._LAST_OUTAGE_SINCE = time.time() - 10 * 60
        ron_daemon._maybe_offer_apology([(123, None)])
        self.assertEqual(len(self.markups), 1)
        self.assertGreater(ron_daemon._apology_last_offer, 0)

    def test_no_offer_without_outage(self):
        # Pure bake drain (degraded never on): no apology offer.
        self._seed_queue([_queue_entry(111)])
        self.assertTrue(ron_daemon._drain_queue())
        self.assertEqual(self.markups, [])

    def test_no_offer_when_queue_empty(self):
        self._start_outage()
        self.assertTrue(ron_daemon._drain_queue())
        self.assertEqual(self.markups, [])

    def test_flap_protection_one_offer_per_hour(self):
        self._seed_queue([_queue_entry(111)])
        self._start_outage()
        ron_daemon._drain_queue()
        self.assertEqual(len(self.markups), 1)
        # Second outage + drain within the hour: no second offer.
        self._seed_queue([_queue_entry(333)])
        self._start_outage(mins_ago=5)
        ron_daemon._drain_queue()
        self.assertEqual(len(self.markups), 1)

    def _yes_callback(self):
        self._seed_queue([_queue_entry(111), _queue_entry(222, conn_id="bc1")])
        self._start_outage()
        ron_daemon._drain_queue()
        ep = next(iter(ron_daemon._apology_pending))
        query = {"id": "q1",
                 "from": {"id": OWNER_ID},
                 "data": f"apology:yes:{ep}",
                 "message": {"chat": {"id": OWNER_ID}}}
        ron_daemon._handle_apology_callback(query, f"apology:yes:{ep}")
        return ep

    def test_yes_sends_apologies(self):
        ep = self._yes_callback()
        apologies = [s for s in self.sends if s[1] == ron_daemon._APOLOGY_TEXT]
        self.assertEqual(len(apologies), 2)
        by_chat = {c: k for c, t, k in apologies}
        self.assertIn(111, by_chat)
        self.assertIn(222, by_chat)
        self.assertEqual(by_chat[222].get("business_connection_id"), "bc1")
        self.assertIsNone(by_chat[111].get("business_connection_id"))
        # Episode consumed; owner got a confirmation.
        self.assertNotIn(ep, ron_daemon._apology_pending)
        self.assertTrue(any("Sent apologies" in t for _, t, _ in self.sends))

    def test_no_sends_nothing(self):
        self._seed_queue([_queue_entry(111)])
        self._start_outage()
        ron_daemon._drain_queue()
        ep = next(iter(ron_daemon._apology_pending))
        query = {"id": "q1", "from": {"id": OWNER_ID},
                 "data": f"apology:no:{ep}",
                 "message": {"chat": {"id": OWNER_ID}}}
        ron_daemon._handle_apology_callback(query, f"apology:no:{ep}")
        apologies = [s for s in self.sends if s[1] == ron_daemon._APOLOGY_TEXT]
        self.assertEqual(apologies, [])
        self.assertTrue(any("no apologies" in t.lower()
                            for _, t, _ in self.sends))

    def test_unknown_episode_expires_gracefully(self):
        query = {"id": "q1", "from": {"id": OWNER_ID},
                 "data": "apology:yes:12345",
                 "message": {"chat": {"id": OWNER_ID}}}
        ron_daemon._handle_apology_callback(query, "apology:yes:12345")
        self.assertTrue(any("expired" in t.lower()
                            for _, t, _ in self.sends))

    def test_non_owner_tap_ignored(self):
        ron_daemon._apology_pending["999"] = [(111, None)]
        query = {"id": "q1", "from": {"id": 555},
                 "data": "apology:yes:999",
                 "message": {"chat": {"id": 555}}}
        ron_daemon._handle_apology_callback(query, "apology:yes:999")
        self.assertEqual(self.sends, [])
        self.assertIn("999", ron_daemon._apology_pending)  # not consumed

    def test_apology_text_is_provider_neutral(self):
        for provider in ("Groq", "Gemini", "OpenRouter"):
            self.assertNotIn(provider, ron_daemon._APOLOGY_TEXT)


class DrainRetryTest(unittest.TestCase):
    """v1.25.2: a transient 429 wave during the drain retries the AI call
    (up to 3 attempts) instead of abandoning the whole queue."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_q = ron_daemon.DEGRADED_QUEUE
        ron_daemon.DEGRADED_QUEUE = os.path.join(self.tmp.name, "dq.jsonl")
        self.calls = []

        def flaky_chat(*a, **k):
            self.calls.append(1)
            if len(self.calls) <= 2:
                raise ron_daemon.ron_ai.RateLimitedError(
                    "rate limited by all AI providers tried")
            return "drained reply"

        patches = [
            mock.patch.object(ron_daemon.ron_ai, "chat", flaky_chat),
            mock.patch.object(ron_daemon, "send_rich_with_kb",
                              lambda *a, **k: None),
            mock.patch.object(ron_daemon, "send_typing",
                              lambda *a, **k: None),
            mock.patch.object(ron_daemon, "group_context", lambda: ""),
            mock.patch.object(ron_daemon, "handle_group_sends",
                              lambda *a, **k: None),
            mock.patch.object(ron_daemon, "handle_send_topic",
                              lambda *a, **k: None),
            mock.patch.object(ron_daemon, "handle_topic_actions",
                              lambda *a, **k: None),
            mock.patch.object(ron_daemon.time, "sleep", lambda s: None),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

    def tearDown(self):
        ron_daemon.DEGRADED_QUEUE = self.orig_q
        self.tmp.cleanup()

    def test_drain_retries_on_429_then_succeeds(self):
        entries = [_queue_entry(111), _queue_entry(222, conn_id="bc1")]
        with open(ron_daemon.DEGRADED_QUEUE, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        replied = []
        self.assertTrue(ron_daemon._drain_queue_inner(replied))
        # First entry: 2x 429 then success = 3 chat calls; second: 1 call.
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(len(replied), 2)
        # Queue file emptied — nothing stuck.
        with open(ron_daemon.DEGRADED_QUEUE) as f:
            self.assertEqual([l for l in f if l.strip()], [])

    def test_drain_gives_up_after_3_429s(self):
        def always_429(*a, **k):
            raise ron_daemon.ron_ai.RateLimitedError(
                "rate limited by all AI providers tried")

        with mock.patch.object(ron_daemon.ron_ai, "chat", always_429):
            with open(ron_daemon.DEGRADED_QUEUE, "w") as f:
                f.write(json.dumps(_queue_entry(111)) + "\n")
            replied = []
            ron_daemon._drain_queue_inner(replied)
            self.assertEqual(replied, [])
            # Entry stays queued for the next drain trigger.
            with open(ron_daemon.DEGRADED_QUEUE) as f:
                self.assertEqual(len([l for l in f if l.strip()]), 1)


if __name__ == "__main__":
    unittest.main()
