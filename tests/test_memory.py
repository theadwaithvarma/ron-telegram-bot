#!/usr/bin/env python3
"""Phase 4 tests: memory store + privacy flows. No network, no Telegram.

Run: python3 tests/test_memory.py  (or python3 -m unittest discover tests)
"""
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.expanduser("~"), "workspace",
                                "telegram-bot"))

from cryptography.fernet import Fernet

import ron_memory
import ron_privacy
import ron_ai
import ron_daemon

TEST_KEY = Fernet.generate_key().decode()


def fresh_store():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return ron_memory.MemoryStore(path=tmp.name), tmp.name


class FakeSends:
    """Capture daemon send side effects."""

    def __init__(self):
        self.group_sends = []   # (chat_id, text, reply_to)
        self.dms = []           # (user_id, text)
        self.dm_kbs = []        # (user_id, keyboard)
        self.dm_blocks = []     # (user_id, blocks) — in-bubble rich buttons
        self.ai_calls = []      # dicts
        self.dm_ok = True

    def install(self):
        self._orig = (ron_daemon.send, ron_daemon.send_markup,
                      ron_daemon.send_rich_with_kb, ron_daemon.ai_reply,
                      ron_daemon.send_blocks)

        def send(chat_id, text, reply_to=None, thread_id=None,
                 business_connection_id=None):
            # Positive int ids are users (DMs); strings/negative are groups.
            if isinstance(chat_id, int) and chat_id > 0:
                self.dms.append((chat_id, text))
            else:
                self.group_sends.append((chat_id, text, reply_to))
            return {"ok": True}

        def send_markup(chat_id, text, reply_markup, reply_to=None,
                        business_connection_id=None):
            # DMs (user ids) vs group sends are distinguished by the
            # caller; record both, tests assert on the right one.
            self.dms.append((chat_id, text))
            self.dm_kbs.append((chat_id, reply_markup))
            return {"ok": self.dm_ok}

        def send_blocks(chat_id, blocks, reply_to=None, thread_id=None,
                        business_connection_id=None):
            # Blocks-form approval DM: text stays readable for assertions.
            texts = [b.get("text") for b in blocks
                     if b.get("type") == "paragraph"]
            self.dms.append((chat_id, "\n\n".join(
                t if isinstance(t, str) else "" for t in texts)))
            self.dm_blocks.append((chat_id, blocks))
            return {"ok": self.dm_ok}

        def send_rich_with_kb(chat_id, reply, reply_to=None, thread_id=None,
                              business_connection_id=None):
            self.group_sends.append((chat_id, reply, reply_to))
            return {"ok": True}

        def ai_reply(chat_id, user_id, user_name, text, private=True,
                     reply_to=None, thread_id=None,
                     business_connection_id=None, cacheable=True):
            self.ai_calls.append({"chat_id": chat_id, "user_id": user_id,
                                  "text": text, "private": private,
                                  "reply_to": reply_to,
                                  "cacheable": cacheable})

        ron_daemon.send = send
        ron_daemon.send_markup = send_markup
        ron_daemon.send_blocks = send_blocks
        ron_daemon.send_rich_with_kb = send_rich_with_kb
        ron_daemon.ai_reply = ai_reply

    def uninstall(self):
        (ron_daemon.send, ron_daemon.send_markup,
         ron_daemon.send_rich_with_kb, ron_daemon.ai_reply,
         ron_daemon.send_blocks) = self._orig


class MemoryStoreTest(unittest.TestCase):
    def setUp(self):
        os.environ[ron_memory.KEY_ENV] = TEST_KEY
        self.store, self.path = fresh_store()
        ron_daemon.mem = self.store

    def tearDown(self):
        self.store.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass
        ron_daemon.mem = None

    # -- core: isolation ------------------------------------------------
    def test_cross_chat_isolation_messages(self):
        self.store.record_message("A", 1, "user", "the purple elephant dances")
        self.store.record_message("B", 2, "user", "nothing relevant here")
        self.assertEqual(self.store.recall("B", "purple elephant"), [])
        hits = self.store.recall("A", "purple elephant")
        self.assertEqual(len(hits), 1)
        self.assertIn("purple elephant", hits[0]["text"])

    def test_cross_chat_isolation_facts(self):
        self.store.remember("A", "Priya loves filter coffee")
        self.assertEqual(self.store.recall_facts("B", "filter coffee"), [])
        hits = self.store.recall_facts("A", "filter coffee")
        self.assertEqual(len(hits), 1)

    def test_memory_context_is_chat_scoped(self):
        self.store.remember("A", "Priya loves filter coffee")
        ctx = ron_daemon._memory_context("B", "filter coffee", True)
        self.assertEqual(ctx, "")
        ctx = ron_daemon._memory_context("A", "filter coffee", True)
        self.assertIn("filter coffee", ctx)

    # -- forget / wipe ---------------------------------------------------
    def test_forget(self):
        self.store.remember("A", "Priya loves filter coffee")
        self.store.remember("A", "Priya hates loud music")
        n = self.store.forget("A", "filter coffee")
        self.assertGreaterEqual(n, 1)
        self.assertEqual(self.store.recall_facts("A", "filter coffee"), [])
        self.assertEqual(len(self.store.recall_facts("A", "loud music")), 1)

    def test_forget_latest_fact(self):
        self.store.remember("A", "Priya loves filter coffee")
        fid = self.store.forget_latest_fact("A")
        self.assertIsNotNone(fid)
        self.assertEqual(self.store.facts("A"), [])

    def test_wipe_chat(self):
        self.store.record_message("X", 1, "user", "hello world")
        self.store.remember("X", "X fact one")
        self.store.record_consent("X", 1, 999)
        self.store.note_member("X", 1, "Zed")
        self.store.create_pending("disclosure", "X", 1)
        out = self.store.wipe_chat("X")
        self.assertGreater(out["messages"], 0)
        self.assertGreater(out["facts"], 0)
        self.assertEqual(self.store.recall("X", "hello"), [])
        self.assertEqual(self.store.facts("X"), [])
        self.assertFalse(self.store.has_consent("X", 1, 999))
        self.assertEqual(self.store.active_pendings_for(1), [])

    # -- expiry -----------------------------------------------------------
    def test_prune_expired(self):
        old = time.time() - 40 * 86400
        self.store.record_message("A", 1, "user", "ancient history", ts=old)
        self.store.record_message("A", 1, "user", "fresh news")
        n = self.store.prune_expired(30)
        self.assertEqual(n, 1)
        self.assertEqual(self.store.recall("A", "ancient history"), [])
        self.assertEqual(len(self.store.recall("A", "fresh news")), 1)

    # -- encryption -------------------------------------------------------
    def test_encryption_at_rest(self):
        secret = "xyzzzy secret phrase nine"
        self.store.record_message("A", 1, "user", secret)
        self.store.remember("A", "fact with " + secret)
        with open(self.path, "rb") as fh:
            raw = fh.read()
        self.assertNotIn(secret.encode(), raw)
        # ...but recall decrypts correctly
        self.assertIn(secret, self.store.recall("A", "xyzzzy")[0]["text"])
        self.assertIn(secret,
                      self.store.recall_facts("A", "xyzzzy")[0]["text"])

    def test_fail_closed_without_key(self):
        os.environ.pop(ron_memory.KEY_ENV, None)
        with self.assertRaises(ron_memory.MemoryStoreError):
            ron_memory.MemoryStore(path=self.path + ".nokey")
        os.environ[ron_memory.KEY_ENV] = TEST_KEY

    def test_passphrase_key_derivation(self):
        os.environ[ron_memory.KEY_ENV] = "a human passphrase"
        s2, p2 = fresh_store()
        try:
            s2.record_message("A", 1, "user", "derived key works")
            self.assertIn("derived",
                          s2.recall("A", "derived")[0]["text"])
        finally:
            s2.close()
            os.unlink(p2)
            os.environ[ron_memory.KEY_ENV] = TEST_KEY

    # -- dual-write parity -------------------------------------------------
    def test_dual_write_parity(self):
        tmp_hist = self.path + ".hist.json"
        orig_hist, orig_hook = ron_ai.HISTORY_FILE, ron_ai.MEMORY_HOOK
        ron_ai.HISTORY_FILE = tmp_hist
        ron_ai.MEMORY_HOOK = ron_daemon._memory_dual_write
        try:
            ron_ai._save_turn("777", "hello bot", "Bob", "hi Bob", user_id=42)
            import json
            with open(tmp_hist) as fh:
                hist = json.load(fh)
            turns = hist["777"]
            self.assertEqual(turns[-2]["content"], "[Bob]: hello bot")
            self.assertEqual(turns[-1]["content"], "hi Bob")
            hits = self.store.recall("777", "hello bot")
            self.assertEqual(len(hits), 1)
            self.assertIn("hello bot", hits[0]["text"])
        finally:
            ron_ai.HISTORY_FILE, ron_ai.MEMORY_HOOK = orig_hist, orig_hook
            try:
                os.unlink(tmp_hist)
            except OSError:
                pass

    # -- contradiction handling --------------------------------------------
    def test_remember_contradiction_replaces(self):
        f1 = self.store.remember("C", "my web tech exam is on monday morning")
        f2 = self.store.remember("C", "my web tech exam moved to tuesday afternoon")
        self.assertNotEqual(f1, f2)
        active = self.store.facts("C")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], f2)
        self.assertIn(str(f1), active[0]["note"])

    def test_remember_duplicate_returns_existing(self):
        f1 = self.store.remember("C", "my web tech exam is on monday morning")
        f2 = self.store.remember("C", "my web tech exam is on monday morning")
        self.assertEqual(f1, f2)
        self.assertEqual(len(self.store.facts("C")), 1)


class PrivacyPureTest(unittest.TestCase):
    def test_classify_response(self):
        self.assertEqual(ron_privacy.classify_response("yes, go ahead"), "approve")
        self.assertEqual(ron_privacy.classify_response("no, keep it private"), "deny")
        self.assertIsNone(ron_privacy.classify_response("what time is it?"))
        # deny wins on conflict
        self.assertEqual(ron_privacy.classify_response("yes no"), "deny")
        self.assertIsNone(ron_privacy.classify_response("x" * 200))

    def test_looks_personal(self):
        self.assertTrue(ron_privacy.looks_personal("what is my phone number"))
        self.assertTrue(ron_privacy.looks_personal("call me on +91 98765 43210"))
        self.assertTrue(ron_privacy.looks_personal("my email is a@b.com"))
        self.assertTrue(ron_privacy.looks_personal("my salary details"))
        self.assertFalse(ron_privacy.looks_personal("what's the weather like"))
        self.assertFalse(ron_privacy.looks_personal("explain recursion"))

    def test_topic_matching(self):
        fact = "Priya carries an epipen for peanut allergy emergencies"
        self.assertTrue(ron_privacy.topic_matches(
            "bring snacks without peanut allergy risk, and an epipen", fact))
        self.assertFalse(ron_privacy.topic_matches(
            "what time is the meeting tomorrow", fact))

    def test_callback_parsing(self):
        self.assertEqual(ron_privacy.parse_memory_callback("mem:go:42"), ("go", 42))
        self.assertEqual(ron_privacy.parse_memory_callback("mem:no:7"), ("no", 7))
        self.assertIsNone(ron_privacy.parse_memory_callback("q:something"))
        self.assertIsNone(ron_privacy.parse_memory_callback("mem:go:"))
        # callback_data stays under Telegram's 64-byte cap
        kb = ron_privacy.approval_keyboard(123456789)
        for row in kb["inline_keyboard"]:
            for b in row:
                self.assertLessEqual(len(b["callback_data"].encode()), 64)

    def test_parse_remember_forget(self):
        self.assertEqual(ron_privacy.parse_remember("remember this: my exam is monday"),
                         "my exam is monday")
        self.assertIsNone(ron_privacy.parse_remember("remember the alamo"))
        self.assertEqual(ron_privacy.parse_forget("forget everything"), ("all", None))
        self.assertEqual(ron_privacy.parse_forget("forget that embarrassing story"),
                         ("query", "embarrassing story"))
        self.assertEqual(ron_privacy.parse_forget("forget that"), ("latest", None))
        self.assertIsNone(ron_privacy.parse_forget("i forget what i was saying"))

    def test_dm_ask_templates_leak_nothing_extra(self):
        dm = ron_privacy.disclosure_dm_text("Study Group", "the secret fact")
        self.assertIn("Study Group", dm)
        # group ask (old pattern) must never be used: no template asks in-group
        self.assertNotIn("okay if I share it here", dm.replace("Okay to share it there?", ""))
        ov = ron_privacy.overshare_dm_text("Study Group", "my phone number")
        self.assertIn("my phone number", ov)  # DM to self: fine


class DisclosureFlowTest(unittest.TestCase):
    """Refinement 3: approval via private DM, never in the group."""

    def setUp(self):
        os.environ[ron_memory.KEY_ENV] = TEST_KEY
        self.store, self.path = fresh_store()
        ron_daemon.mem = self.store
        self.fake = FakeSends()
        self.fake.install()
        # P (user 111) had a DM with the bot; P is also in group G1.
        self.store.note_member("G1", 111, "Priya")
        self.fact_text = ("Priya carries an epipen for peanut allergy emergencies")
        self.fid = self.store.remember("111", self.fact_text)
        self.msg = ("hey team, bring snacks without peanut allergy risk, "
                    "and someone carry an epipen just in case")

    def tearDown(self):
        self.fake.uninstall()
        self.store.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass
        ron_daemon.mem = None

    def test_a_trigger_sends_dm_not_group_message(self):
        """(a) group trigger -> zero public bot message, DM sent to P."""
        ron_daemon._maybe_trigger_disclosures(
            "G1", self.msg, 999, "Test Group", allow_disclose=False)
        # zero public bot messages
        self.assertEqual(self.fake.group_sends, [])
        # exactly one DM to P with Yes/No buttons, in-bubble (blocks form)
        self.assertEqual(len(self.fake.dms), 1)
        uid, dm_text = self.fake.dms[0]
        self.assertEqual(uid, 111)
        self.assertEqual(len(self.fake.dm_blocks), 1)
        blocks = self.fake.dm_blocks[0][1]
        btn_blocks = [b for b in blocks if b.get("type") == "buttons"]
        self.assertEqual(len(btn_blocks), 1)
        cbs = [b["callback_data"] for b in btn_blocks[0]["buttons"]]
        self.assertTrue(any(c.startswith("mem:go:") for c in cbs))
        self.assertTrue(any(c.startswith("mem:no:") for c in cbs))
        # pending recorded
        pend = self.store.active_pendings_for(111, group_chat_id="G1")
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["kind"], "disclosure")

    def test_b_approval_discloses_exactly_once(self):
        """(b) P approves in DM -> exactly one group disclosure + consent."""
        ron_daemon._maybe_trigger_disclosures(
            "G1", self.msg, 999, "Test Group", allow_disclose=False)
        pend = self.store.active_pendings_for(111, group_chat_id="G1")[0]
        self.fake.dms.clear(); self.fake.dm_kbs.clear()
        self.fake.dm_blocks.clear()
        ron_daemon._apply_approval(pend, approved=True)
        # exactly one disclosure in the group, as a reply to the trigger
        self.assertEqual(len(self.fake.group_sends), 1)
        cid, text, reply_to = self.fake.group_sends[0]
        self.assertEqual(cid, "G1")
        self.assertEqual(reply_to, 999)
        self.assertIn("epipen", text)
        # consent recorded; pending closed
        self.assertTrue(self.store.has_consent("G1", 111, self.fid))
        self.assertEqual(self.store.get_pending(pend["id"])["status"],
                         "disclosed")

    def test_c_decline_means_silence(self):
        """(c) decline -> nothing public, brief private note."""
        ron_daemon._maybe_trigger_disclosures(
            "G1", self.msg, 999, "Test Group", allow_disclose=False)
        pend = self.store.active_pendings_for(111, group_chat_id="G1")[0]
        self.fake.dms.clear(); self.fake.group_sends.clear()
        ron_daemon._apply_approval(pend, approved=False)
        self.assertEqual(self.fake.group_sends, [])
        self.assertEqual(len(self.fake.dms), 1)
        self.assertIn("private", self.fake.dms[0][1].lower())
        self.assertFalse(self.store.has_consent("G1", 111, self.fid))

    def test_c_timeout_means_silence(self):
        pid = self.store.create_pending("disclosure", "G1", 111,
                                        group_message_id=999,
                                        fact_id=self.fid, dm_chat_id="111",
                                        timeout_secs=0)
        expired = self.store.expire_due()
        self.assertIn(pid, expired)
        self.assertEqual(self.store.active_pendings_for(111), [])
        self.assertEqual(self.fake.group_sends, [])

    def test_d_unreachable_stays_silent(self):
        """(d) P unreachable via DM -> silent, logged without content."""
        self.fake.dm_ok = False
        ron_daemon._maybe_trigger_disclosures(
            "G1", self.msg, 999, "Test Group", allow_disclose=False)
        self.assertEqual(self.fake.group_sends, [])
        # the DM attempts failed (blocks path, then keyboard fallback) ->
        # pending marked unreachable, still silent
        rows = self.store._db.execute(
            "SELECT id, status FROM pending_approvals").fetchall()
        statuses = [r[1] for r in rows]
        self.assertIn("unreachable", statuses)
        self.assertEqual(len(self.fake.dms), 2)  # both failed attempts only

    def test_e_consent_means_no_reask(self):
        """(e) consent recorded -> second trigger discloses, never re-asks."""
        self.store.record_consent("G1", 111, self.fid)
        # ask phase: no DM, no pending
        ron_daemon._maybe_trigger_disclosures(
            "G1", self.msg, 1001, "Test Group", allow_disclose=False)
        self.assertEqual(self.fake.dms, [])
        self.assertEqual(self.fake.group_sends, [])
        # engaged phase: discloses directly, still no DM ask
        ron_daemon._maybe_trigger_disclosures(
            "G1", self.msg, 1001, "Test Group", allow_disclose=True)
        self.assertEqual(self.fake.dms, [])
        self.assertEqual(len(self.fake.group_sends), 1)
        self.assertEqual(self.fake.group_sends[0][2], 1001)

    def test_no_pending_duplication(self):
        ron_daemon._maybe_trigger_disclosures(
            "G1", self.msg, 999, "Test Group", allow_disclose=False)
        self.fake.dms.clear()
        ron_daemon._maybe_trigger_disclosures(
            "G1", self.msg, 1000, "Test Group", allow_disclose=False)
        # second trigger while ask is pending: no second DM
        self.assertEqual(self.fake.dms, [])
        self.assertEqual(
            len(self.store.active_pendings_for(111, group_chat_id="G1")), 1)

    def test_group_yes_is_not_consent(self):
        """A 'yes' typed in the group is ambiguous chat, never approval."""
        ron_daemon._maybe_trigger_disclosures(
            "G1", self.msg, 999, "Test Group", allow_disclose=False)
        pend = self.store.active_pendings_for(111, group_chat_id="G1")[0]
        # P chats "yes" in the group (answering someone else) — the
        # pending must survive and nothing must be disclosed.
        ron_daemon.handle_update({"message": {
            "message_id": 1002,
            "chat": {"id": "G1", "type": "supergroup", "title": "Test Group"},
            "from": {"id": 111, "first_name": "Priya"},
            "text": "yes, sounds good",
        }})
        self.assertEqual(self.store.get_pending(pend["id"])["status"],
                         "pending")
        self.assertEqual(self.fake.group_sends, [])
        self.assertFalse(self.store.has_consent("G1", 111, self.fid))

    def test_callback_wrong_user_cannot_approve(self):
        ron_daemon._maybe_trigger_disclosures(
            "G1", self.msg, 999, "Test Group", allow_disclose=False)
        pend = self.store.active_pendings_for(111, group_chat_id="G1")[0]
        query = {"id": "q1", "from": {"id": 222},  # not P
                 "data": f"mem:go:{pend['id']}"}
        ron_daemon._resolve_callback_approval(query, "go", pend["id"])
        self.assertEqual(self.store.get_pending(pend["id"])["status"],
                         "pending")
        self.assertEqual(self.fake.group_sends, [])

    def test_callback_right_user_approves(self):
        ron_daemon._maybe_trigger_disclosures(
            "G1", self.msg, 999, "Test Group", allow_disclose=False)
        pend = self.store.active_pendings_for(111, group_chat_id="G1")[0]
        query = {"id": "q1", "from": {"id": 111},
                 "data": f"mem:go:{pend['id']}",
                 "message": {"chat": {"id": 111, "type": "private"}}}
        ron_daemon._resolve_callback_approval(query, "go", pend["id"])
        self.assertEqual(len(self.fake.group_sends), 1)
        self.assertTrue(self.store.has_consent("G1", 111, self.fid))


class OvershareGuardTest(unittest.TestCase):
    def setUp(self):
        os.environ[ron_memory.KEY_ENV] = TEST_KEY
        self.store, self.path = fresh_store()
        ron_daemon.mem = self.store
        self.fake = FakeSends()
        self.fake.install()
        self.owner = ron_daemon.OWNER_ID

    def tearDown(self):
        self.fake.uninstall()
        self.store.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass
        ron_daemon.mem = None

    def _group_update(self, text, user_id=None):
        uid = user_id if user_id is not None else self.owner
        return {"message": {
            "message_id": 55,
            "chat": {"id": -1001, "type": "supergroup", "title": "Test Group"},
            "from": {"id": uid, "first_name": "Adwaith"},
            "text": f"@ronrebootedbot {text}",
            "entities": [{"type": "mention", "offset": 0, "length": 15}],
        }}

    def test_a_personal_question_gets_private_ask(self):
        """(a) personal question in group -> private DM ask, zero content leaked publicly."""
        ron_daemon.handle_update(self._group_update("what is my phone number"))
        # nothing public
        self.assertEqual(self.fake.group_sends, [])
        self.assertEqual(self.fake.ai_calls, [])
        # one DM to the owner with Yes/No, in-bubble (blocks form)
        self.assertEqual(len(self.fake.dms), 1)
        uid, dm_text = self.fake.dms[0]
        self.assertEqual(uid, self.owner)
        self.assertIn("personal", dm_text)
        self.assertEqual(len(self.fake.dm_blocks), 1)
        blocks = self.fake.dm_blocks[0][1]
        btn_blocks = [b for b in blocks if b.get("type") == "buttons"]
        self.assertEqual(len(btn_blocks), 1)
        cbs = [b["callback_data"] for b in btn_blocks[0]["buttons"]]
        self.assertTrue(any(c.startswith("mem:go:") for c in cbs))
        pend = self.store.active_pendings_for(self.owner)
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["kind"], "overshare")

    def test_b_approval_answers_once_in_group(self):
        """(b) explicit approval -> answer given once, in the group."""
        ron_daemon.handle_update(self._group_update("what is my phone number"))
        pend = self.store.active_pendings_for(self.owner)[0]
        self.fake.dms.clear()
        ron_daemon._apply_approval(pend, approved=True)
        self.assertEqual(len(self.fake.ai_calls), 1)
        call = self.fake.ai_calls[0]
        self.assertEqual(call["chat_id"], "-1001")
        self.assertFalse(call["private"])
        self.assertEqual(call["reply_to"], 55)
        self.assertIn("phone number", call["text"])
        # Phase 6: the overshare answer may draw on personal DM facts, so
        # it must bypass the answer cache entirely.
        self.assertFalse(call["cacheable"])

    def test_c_decline_means_silence(self):
        ron_daemon.handle_update(self._group_update("what is my phone number"))
        pend = self.store.active_pendings_for(self.owner)[0]
        self.fake.dms.clear(); self.fake.ai_calls.clear()
        ron_daemon._apply_approval(pend, approved=False)
        self.assertEqual(self.fake.ai_calls, [])
        self.assertEqual(self.fake.group_sends, [])
        self.assertEqual(len(self.fake.dms), 1)  # "kept private" DM only

    def test_d_dm_has_no_guard(self):
        """(d) same question in DM -> answered directly, no approval dance."""
        ron_daemon.handle_update({"message": {
            "message_id": 1,
            "chat": {"id": self.owner, "type": "private"},
            "from": {"id": self.owner, "first_name": "Adwaith"},
            "text": "what is my phone number",
        }})
        self.assertEqual(len(self.fake.ai_calls), 1)
        self.assertTrue(self.fake.ai_calls[0]["private"])
        self.assertEqual(self.fake.dms, [])

    def test_nonpersonal_group_question_unaffected(self):
        ron_daemon.handle_update(self._group_update("explain recursion simply"))
        self.assertEqual(len(self.fake.ai_calls), 1)
        self.assertEqual(self.fake.dms, [])

    def test_nonowner_personal_question_not_guarded(self):
        # guard is scoped to Adwaith (the refinement's scope)
        ron_daemon.handle_update(self._group_update("what is my phone number",
                                                    user_id=999))
        self.assertEqual(len(self.fake.ai_calls), 1)


class NaturalCommandTest(unittest.TestCase):
    def setUp(self):
        os.environ[ron_memory.KEY_ENV] = TEST_KEY
        self.store, self.path = fresh_store()
        ron_daemon.mem = self.store
        self.fake = FakeSends()
        self.fake.install()
        # isolate JSON history + enable the SQLite dual-write hook
        self._tmp_hist = self.path + ".hist.json"
        self._orig_hist = ron_ai.HISTORY_FILE
        self._orig_hook = ron_ai.MEMORY_HOOK
        ron_ai.HISTORY_FILE = self._tmp_hist
        ron_ai.MEMORY_HOOK = ron_daemon._memory_dual_write

    def tearDown(self):
        self.fake.uninstall()
        self.store.close()
        ron_ai.HISTORY_FILE = self._orig_hist
        ron_ai.MEMORY_HOOK = self._orig_hook
        for p in (self.path, self._tmp_hist):
            try:
                os.unlink(p)
            except OSError:
                pass
        ron_daemon.mem = None

    def test_remember_and_forget_flow(self):
        self.assertTrue(ron_daemon._handle_memory_nl(
            "D1", 42, "Bob", "remember this: my dog is called Bruno"))
        self.assertEqual(len(self.store.recall_facts("D1", "dog Bruno")), 1)
        # the ack itself dual-writes (JSON + SQLite)
        self.assertEqual(len(self.store.recall("D1", "Noted")), 1)
        self.assertTrue(ron_daemon._handle_memory_nl(
            "D1", 42, "Bob", "forget that dog"))
        self.assertEqual(self.store.recall_facts("D1", "dog Bruno"), [])

    def test_forget_command_authorization(self):
        """Per-chat /forget: DM anyone wipes own DM; group only owner."""
        self.store.remember("G9", "group fact here")
        # non-owner in group: denied, memory intact
        self.assertTrue(ron_daemon.handle_command(
            "G9", 999, "forget", False, "/forget", "Bob"))
        self.assertTrue(any("Only Adwaith" in t
                            for _, t, _ in self.fake.group_sends))
        self.assertEqual(len(self.store.recall_facts("G9", "group fact")), 1)
        # owner in group: wiped
        self.fake.group_sends.clear()
        self.assertTrue(ron_daemon.handle_command(
            "G9", ron_daemon.OWNER_ID, "forget", False, "/forget", "Adwaith"))
        self.assertEqual(self.store.recall_facts("G9", "group fact"), [])
        # DM: any user wipes their own DM
        self.store.remember("4242", "dm fact here")
        self.assertTrue(ron_daemon.handle_command(
            "4242", 4242, "forget", True, "/forget", "Zed"))
        self.assertEqual(self.store.recall_facts("4242", "dm fact"), [])

    def test_forget_everything_wipes(self):
        self.store.remember("D1", "my dog is called Bruno")
        self.store.record_message("D1", 42, "user", "hello")
        self.assertTrue(ron_daemon._handle_memory_nl(
            "D1", 42, "Bob", "forget everything"))
        self.assertEqual(self.store.facts("D1"), [])
        self.assertEqual(self.store.recall("D1", "hello"), [])


class Phase5Test(unittest.TestCase):
    """Phase 5: negation supersede, chains, complete forgetting, ranking."""

    def setUp(self):
        os.environ[ron_memory.KEY_ENV] = TEST_KEY
        self.store, self.path = fresh_store()
        ron_daemon.mem = self.store

    def tearDown(self):
        self.store.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass
        ron_daemon.mem = None

    # -- negation-aware contradiction -----------------------------------
    def test_negation_supersedes(self):
        f1 = self.store.remember("C", "my exam is on monday")
        f2 = self.store.remember("C", "the exam is not happening")
        self.assertNotEqual(f1, f2)
        active = self.store.facts("C")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], f2)
        self.assertIn(str(f1), active[0]["note"])
        self.assertIn("(negation)", active[0]["note"])

    def test_negation_nt_form_supersedes(self):
        f1 = self.store.remember("C", "the standup is friday")
        f2 = self.store.remember("C", "standup isn't happening")
        active = self.store.facts("C")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], f2)
        self.assertIn("(negation)", active[0]["note"])

    def test_single_shared_word_without_negation_kept(self):
        # 1 shared content word, no change marker, no negation -> both kept.
        self.store.remember("C", "my exam is on monday")
        self.store.remember("C", "the exam results arrived")
        self.assertEqual(len(self.store.facts("C")), 2)

    def test_supersede_keeps_fact_kind_column(self):
        # the supersede rule label must not leak into the fact's kind column
        self.store.remember("C", "my exam is on monday", kind="preference")
        self.store.remember("C", "the exam is not happening",
                            kind="preference")
        active = self.store.facts("C")
        self.assertEqual(len(active), 1)
        row = self.store._db.execute(
            "SELECT kind FROM facts WHERE id=?",
            (active[0]["id"],)).fetchone()
        self.assertEqual(row[0], "preference")

    def test_old_rules_still_fire(self):
        # 2+ shared words, no negation anywhere -> classic supersede.
        f1 = self.store.remember("C", "my web tech exam is on monday morning")
        f2 = self.store.remember("C", "my web tech exam moved to tuesday")
        active = self.store.facts("C")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], f2)
        self.assertIn("(contradiction/update)", active[0]["note"])

    # -- supersession chains ----------------------------------------------
    def _three_chain(self, chat="E"):
        f1 = self.store.remember(chat, "my flight is on friday")
        f2 = self.store.remember(chat, "my flight moved to saturday")
        f3 = self.store.remember(chat, "my flight is not saturday")
        return f1, f2, f3

    def test_supersession_chain_order(self):
        f1, f2, f3 = self._three_chain()
        for start in (f1, f2, f3):
            chain = self.store.supersession_chain("E", start)
            self.assertEqual([f["id"] for f in chain], [f1, f2, f3])
        # links point forward: f1 -> f2 -> f3, newest has no successor
        self.assertEqual(chain[0]["superseded_by"], f2)
        self.assertEqual(chain[1]["superseded_by"], f3)
        self.assertIsNone(chain[2]["superseded_by"])

    def test_supersession_chain_chat_scoped(self):
        f1, _f2, _f3 = self._three_chain()
        self.assertEqual(self.store.supersession_chain("OTHER", f1), [])
        self.assertEqual(self.store.supersession_chain("E", 999999), [])

    def test_supersession_chain_singleton(self):
        f1 = self.store.remember("E", "lone fact standing")
        chain = self.store.supersession_chain("E", f1)
        self.assertEqual([f["id"] for f in chain], [f1])

    # -- complete forgetting -----------------------------------------------
    def test_forget_cascade_deletes_chain_and_orphans(self):
        f1, f2, f3 = self._three_chain()
        self.store.record_consent("G1", 111, f1)
        pid = self.store.create_pending("disclosure", "G1", 111,
                                        fact_id=f2)
        # same text in another chat must survive (isolation)
        self.store.remember("OTHER", "my flight is on friday")
        n = self.store.forget("E", "flight friday")
        self.assertGreaterEqual(n, 3)  # int return, daemon compatible
        self.assertEqual(self.store.facts("E"), [])
        self.assertFalse(self.store.has_consent("G1", 111, f1))
        self.assertIsNone(self.store.get_pending(pid))
        self.assertEqual(self.store.active_pendings_for(111), [])
        self.assertEqual(len(self.store.facts("OTHER")), 1)

    def test_forget_detailed_breakdown(self):
        f1, f2, _f3 = self._three_chain()
        self.store.record_consent("G1", 111, f1)
        self.store.create_pending("disclosure", "G1", 111, fact_id=f2)
        self.store.record_message("E", 1, "user", "flight booking confirmed")
        d = self.store.forget_detailed("E", "flight")
        self.assertEqual(set(d), {"facts", "messages", "consents",
                                 "pendings"})
        self.assertEqual(d["facts"], 3)
        self.assertEqual(d["messages"], 1)
        self.assertEqual(d["consents"], 1)
        self.assertEqual(d["pendings"], 1)
        # forget() returns the same total as an int
        self._three_chain(chat="E2")
        self.store.record_message("E2", 1, "user", "flight delayed again")
        expected = sum(self.store.forget_detailed("E2", "flight").values())
        self._three_chain(chat="E3")
        self.store.record_message("E3", 1, "user", "flight delayed again")
        n3 = self.store.forget("E3", "flight")
        self.assertIsInstance(n3, int)
        self.assertEqual(n3, expected)
        self.assertEqual(self.store.forget("E3", "zzzznomatch"), 0)

    # -- richer retrieval ---------------------------------------------------
    def test_recall_ranked_relevance_beats_recency(self):
        self.store.record_message("R", 1, "user",
                                  "final exam syllabus and dates announced",
                                  ts=1000)
        self.store.record_message("R", 1, "user",
                                  "lunch menu for the canteen", ts=2000)
        # AND over all three terms matches nothing -> OR fallback;
        # the older message matches 2 terms vs 1 -> wins on relevance.
        hits = self.store.recall_ranked("R", "exam syllabus lunch")
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0]["ts"], 1000)
        self.assertIn("exam syllabus", hits[0]["text"])

    def test_recall_ranked_recency_breaks_ties(self):
        self.store.record_message("R", 1, "user", "exam at nine", ts=1000)
        self.store.record_message("R", 1, "user", "exam at nine", ts=2000)
        hits = self.store.recall_ranked("R", "exam nine")
        self.assertEqual(len(hits), 2)
        # identical text -> identical bm25 -> newer wins the tie
        self.assertEqual(hits[0]["ts"], 2000)

    def test_recall_or_fallback(self):
        self.store.record_message("R", 1, "user", "buy milk tomorrow",
                                  ts=1000)
        self.store.record_message("R", 1, "user", "call mom tonight",
                                  ts=2000)
        # no single message has both terms -> AND empty -> OR surfaces both
        hits = self.store.recall_ranked("R", "milk mom")
        self.assertEqual(len(hits), 2)

    def test_recall_all_unified_and_isolated(self):
        self.store.remember("A", "Priya loves filter coffee")
        self.store.record_message("A", 1, "user",
                                  "grab filter coffee after lunch", ts=1000)
        self.store.remember("B", "Arjun hates filter coffee")
        self.store.record_message("B", 2, "user",
                                  "filter coffee is overrated", ts=1000)
        items = self.store.recall_all("A", "filter coffee", limit=5)
        self.assertGreaterEqual(len(items), 2)
        kinds = [i["kind"] for i in items]
        self.assertIn("fact", kinds)
        self.assertIn("message", kinds)
        # facts weighted above messages
        self.assertLess(kinds.index("fact"), kinds.index("message"))
        # chat isolation: nothing from B leaks in
        for i in items:
            self.assertNotIn("Arjun", i["text"])
            self.assertNotIn("overrated", i["text"])
        self.assertTrue(all(i["kind"] in ("fact", "message") for i in items))

    def test_recall_all_chat_scoped(self):
        self.store.remember("A", "Priya loves filter coffee")
        self.assertEqual(self.store.recall_all("B", "filter coffee"), [])

    def test_memory_context_uses_recall_all(self):
        self.store.remember("A", "Priya loves filter coffee")
        self.store.record_message("A", 1, "user",
                                  "grab filter coffee after lunch")
        ctx = ron_daemon._memory_context("A", "filter coffee", True)
        self.assertIn("filter coffee", ctx)
        self.assertIn("Relevant memory from this chat", ctx)
        self.assertEqual(ron_daemon._memory_context("B", "filter coffee",
                                                    True), "")


class WipeUserTest(unittest.TestCase):
    """wipe_user(): the user's turns everywhere + their whole private-DM
    chat (facts, summaries, bot replies); other chats' facts survive."""

    def setUp(self):
        os.environ[ron_memory.KEY_ENV] = TEST_KEY
        self.store, self.path = fresh_store()

    def tearDown(self):
        self.store.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _seed(self):
        s = self.store
        # User 42's private DM (chat_id "42"): full conversation.
        s.record_message("42", 42, "user", "my exam is Monday")
        s.record_message("42", 0, "assistant", "noted, good luck")
        dm_fact = s.remember("42", "42 has an exam on Monday")
        s.set_meta("42", "summary", "exam prep chat")
        s.record_consent("42", 42, dm_fact)
        s.note_member("42", 42, "Kaviya")
        # User 42 also spoke in group "-100".
        s.record_message("-100", 42, "user", "hey all")
        s.record_message("-100", 0, "assistant", "welcome")
        s.record_message("-100", 7, "user", "other person here")
        group_fact = s.remember("-100", "42 likes filter coffee")
        s.record_consent("-100", 42, group_fact)
        s.note_member("-100", 42, "Kaviya")
        # A pending approval targeting 42.
        s.create_pending("overshare", "-100", 42, fact_id=group_fact,
                         dm_chat_id="42", payload="share?")
        # Someone else's DM is untouched.
        s.record_message("77", 77, "user", "private stuff")
        s.remember("77", "77 is learning guitar")
        return dm_fact, group_fact

    def test_wipe_user_scoping(self):
        dm_fact, group_fact = self._seed()
        out = self.store.wipe_user(42)
        self.assertGreater(out["messages"], 0)
        self.assertGreater(out["facts"], 0)
        self.assertGreater(out["meta"], 0)
        # DM chat fully gone: messages, facts, meta, consent, member.
        self.assertEqual(
            self.store._db.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id='42'"
            ).fetchone()[0], 0)
        self.assertEqual(
            self.store._db.execute(
                "SELECT COUNT(*) FROM facts WHERE chat_id='42'"
            ).fetchone()[0], 0)
        self.assertEqual(
            self.store._db.execute(
                "SELECT COUNT(*) FROM chat_meta WHERE chat_id='42'"
            ).fetchone()[0], 0)
        self.assertEqual(
            self.store._db.execute(
                "SELECT COUNT(*) FROM chat_members WHERE user_id=42"
            ).fetchone()[0], 0)
        # 42's group turn is gone, but the group's fact and other users'
        # turns survive (facts are chat-scoped, not user-attributed).
        self.assertEqual(
            self.store._db.execute(
                "SELECT COUNT(*) FROM messages WHERE user_id=42"
            ).fetchone()[0], 0)
        self.assertEqual(
            self.store._db.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id='-100'"
            ).fetchone()[0], 2)  # bot reply + user 7
        self.assertEqual(
            self.store._db.execute(
                "SELECT COUNT(*) FROM facts WHERE chat_id='-100'"
            ).fetchone()[0], 1)
        items = self.store.recall_all("-100", "filter coffee", limit=5)
        self.assertTrue(any(i["kind"] == "fact" for i in items))
        # Pending approval targeting 42 is gone.
        self.assertEqual(
            self.store._db.execute(
                "SELECT COUNT(*) FROM pending_approvals"
                " WHERE target_user_id=42"
            ).fetchone()[0], 0)
        # Unrelated user's DM untouched.
        self.assertEqual(
            self.store._db.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id='77'"
            ).fetchone()[0], 1)
        self.assertEqual(
            self.store._db.execute(
                "SELECT COUNT(*) FROM facts WHERE chat_id='77'"
            ).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
