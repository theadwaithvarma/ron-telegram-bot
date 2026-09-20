#!/usr/bin/env python3
"""Owner power commands (v1.12.0) tests: gating, bans, broadcast two-step,
/quiet expiry, /forgetuser scoping, ephemeral auto-delete, /model pinning,
clean-restart flag, and the ron_ai hooks (AI_CALL_HOOK, user_id tagging).

No network, no Telegram. Run: python3 -m unittest discover tests -v
(from ~/workspace/ron-telegram-bot)
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_owner
import ron_daemon

OWNER = ron_daemon.OWNER_ID
OTHER = 999888777  # a random non-owner user
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _load_repo_ron_ai():
    """The repo's ron_ai (the daemon's sys.path tweak would otherwise
    resolve the live dir's older copy)."""
    spec = importlib.util.spec_from_file_location(
        "ron_ai_repo", os.path.join(REPO, "ron_ai.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeTimer:
    """Captures (secs, fn, args) instead of waiting."""
    scheduled = []

    def __init__(self, secs, fn, args=()):
        FakeTimer.scheduled.append((secs, fn, args))
        self.daemon = False

    def start(self):
        pass


class PowerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ron_owner_test_")
        ron_owner.set_dir(self.tmp)
        # Fresh in-memory state for every test.
        with ron_owner._LOCK:
            ron_owner._stats.clear()
            ron_owner._users.clear()
            ron_owner._chats.clear()
            ron_owner._bans.clear()
            ron_owner._BROADCAST.clear()
        self.sent = []
        self.orig_send = ron_daemon.send
        self.orig_api = ron_daemon.api
        self.orig_timer = ron_daemon._Timer
        ron_daemon.send = lambda chat_id, text, **k: self.sent.append(
            (chat_id, text)) or {"ok": True,
                                 "result": {"message_id": 4242}}
        ron_daemon.api = lambda *a, **k: {"ok": True}
        ron_daemon._Timer = FakeTimer
        FakeTimer.scheduled.clear()
        with ron_daemon._ACTIVITY_LOCK:
            ron_daemon._ACTIVITY.clear()

    def tearDown(self):
        ron_daemon.send = self.orig_send
        ron_daemon.api = self.orig_api
        ron_daemon._Timer = self.orig_timer
        ron_daemon._SHUTDOWN.clear()
        with ron_daemon._ACTIVITY_LOCK:
            ron_daemon._ACTIVITY.clear()

    def reply_text(self):
        self.assertTrue(self.sent, "expected an owner reply to be sent")
        return self.sent[-1][1]

    def owner(self, cmd, text, **kw):
        kw.setdefault("cmd_msg_id", 7)
        return ron_daemon.handle_command(1, OWNER, cmd, True, text, **kw)


class OwnerGatingTest(PowerBase):
    """Every power command refuses non-owners, ephemerally."""

    def test_all_commands_refuse_nonowner(self):
        cmds = [("stats", "/stats"), ("ban", "/ban 123"),
                ("unban", "/unban 123"), ("broadcast", "/broadcast hi"),
                ("say", "/say 123 hi"), ("restart", "/restart"),
                ("quiet", "/quiet 30"), ("logs", "/logs"),
                ("users", "/users"), ("forgetuser", "/forgetuser 123"),
                ("model", "/model")]
        for cmd, text in cmds:
            with self.subTest(cmd=cmd):
                self.sent.clear()
                FakeTimer.scheduled.clear()
                ok = ron_daemon.handle_command(1, OTHER, cmd, True, text,
                                               cmd_msg_id=7)
                self.assertTrue(ok)
                self.assertIn("Only Adwaith", self.reply_text())
                # Ephemeral: a deletion was scheduled for reply + command.
                self.assertEqual(len(FakeTimer.scheduled), 1)
                secs, _fn, args = FakeTimer.scheduled[0]
                self.assertEqual(secs, ron_owner.EPHEMERAL_TTL)
                self.assertEqual(args[1], [4242, 7])

    def test_ban_does_not_fire_for_nonowner(self):
        ron_daemon.handle_command(1, OTHER, "ban", True, "/ban 123")
        self.assertFalse(ron_owner.is_banned(123))


class EphemeralTest(PowerBase):
    def test_deletion_targets(self):
        ids = ron_owner.deletion_targets(
            {"ok": True, "result": {"message_id": 42}}, 7)
        self.assertEqual(ids, [42, 7])

    def test_deletion_targets_no_reply_id(self):
        self.assertEqual(ron_owner.deletion_targets(None, 7), [7])
        self.assertEqual(ron_owner.deletion_targets({"ok": False}, None),
                         [])

    def test_sweep_deletes_both(self):
        calls = []
        ron_daemon.api = lambda method, params=None, **k: calls.append(
            (method, params)) or {"ok": True}
        ron_daemon._sweep_owner_msgs(123, [42, 7])
        self.assertEqual(
            calls,
            [("deleteMessage", {"chat_id": 123, "message_id": 42}),
             ("deleteMessage", {"chat_id": 123, "message_id": 7})])

    def test_sweep_ignores_failures(self):
        def boom(*a, **k):
            raise RuntimeError("no rights")
        ron_daemon.api = boom
        ron_daemon._sweep_owner_msgs(123, [42, 7])  # must not raise

    def test_ttl_zero_disables_scheduling(self):
        orig = ron_owner.EPHEMERAL_TTL
        ron_owner.EPHEMERAL_TTL = 0
        try:
            ron_daemon._owner_reply(1, OWNER, 7, "hi")
        finally:
            ron_owner.EPHEMERAL_TTL = orig
        self.assertEqual(FakeTimer.scheduled, [])


class BanTest(PowerBase):
    def test_ban_unban_roundtrip(self):
        self.assertFalse(ron_owner.is_banned(123))
        ron_owner.ban(123, OWNER)
        self.assertTrue(ron_owner.is_banned(123))
        self.assertTrue(ron_owner.unban(123))
        self.assertFalse(ron_owner.is_banned(123))
        self.assertFalse(ron_owner.unban(123))  # not banned -> False

    def test_ban_persists_across_reload(self):
        ron_owner.ban(123, OWNER)
        with ron_owner._LOCK:
            ron_owner._bans.clear()
        ron_owner.set_dir(self.tmp)  # reload from disk
        self.assertTrue(ron_owner.is_banned(123))

    def test_ban_command_flow(self):
        self.owner("ban", "/ban 555")
        self.assertIn("Banned 555", self.reply_text())
        self.assertTrue(ron_owner.is_banned(555))
        self.owner("unban", "/unban 555")
        self.assertIn("Unbanned 555", self.reply_text())
        self.owner("unban", "/unban 555")
        self.assertIn("wasn't banned", self.reply_text())

    def test_ban_rejects_bad_input_and_self(self):
        self.owner("ban", "/ban")
        self.assertIn("Usage", self.reply_text())
        self.owner("ban", "/ban abc")
        self.assertIn("Usage", self.reply_text())
        self.owner("ban", f"/ban {OWNER}")
        self.assertIn("Can't ban yourself", self.reply_text())
        self.assertFalse(ron_owner.is_banned(OWNER))

    def test_banned_user_silenced_in_update(self):
        ron_owner.ban(OTHER, OWNER)
        orig_ai = ron_daemon.ai_reply
        ron_daemon.ai_reply = lambda *a, **k: self.fail("AI must not run")
        try:
            ron_daemon.handle_update(
                {"update_id": 1,
                 "message": {"message_id": 9,
                             "chat": {"id": 123, "type": "private"},
                             "from": {"id": OTHER, "first_name": "X"},
                             "text": "hello"}})
        finally:
            ron_daemon.ai_reply = orig_ai
        # Total silence: no reply, no activity ledger entry.
        self.assertEqual(self.sent, [])
        self.assertNotIn(OTHER, ron_daemon._ACTIVITY)

    def test_banned_user_silenced_on_callback(self):
        ron_owner.ban(OTHER, OWNER)
        ron_daemon.handle_callback(
            {"id": "cb1",
             "from": {"id": OTHER, "first_name": "X"},
             "data": "q:hi",
             "message": {"message_id": 5,
                         "chat": {"id": -1001, "type": "group",
                                  "title": "G"}}})
        self.assertNotIn(OTHER, ron_daemon._ACTIVITY)


class BroadcastTest(PowerBase):
    def test_two_step_confirm(self):
        ron_owner.note_traffic(1, "A", None, 100, None, "private")
        ron_owner.note_traffic(2, "B", None, 200, None, "private")
        n, preview = ron_owner.broadcast_request("hello all", OWNER)
        self.assertEqual(n, 2)
        self.assertIn("hello all", preview)
        # Wrong user can't confirm.
        kind, msg = ron_owner.broadcast_confirm(OTHER)
        self.assertEqual(kind, "error")
        # Owner confirms within the window.
        kind, text, chats = ron_owner.broadcast_confirm(OWNER)
        self.assertEqual(kind, "ok")
        self.assertEqual(text, "hello all")
        self.assertEqual(sorted(chats), ["100", "200"])
        # Second confirm finds nothing pending.
        kind, _ = ron_owner.broadcast_confirm(OWNER)
        self.assertEqual(kind, "error")

    def test_confirm_expires(self):
        ron_owner.note_traffic(1, "A", None, 100, None, "private")
        ron_owner.broadcast_request("hi", OWNER)
        with ron_owner._LOCK:
            ron_owner._BROADCAST["ts"] -= (ron_owner.BROADCAST_WINDOW + 1)
        kind, msg = ron_owner.broadcast_confirm(OWNER)
        self.assertEqual(kind, "error")
        self.assertIn("expired", msg)

    def test_cancel(self):
        ron_owner.note_traffic(1, "A", None, 100, None, "private")
        ron_owner.broadcast_request("hi", OWNER)
        self.assertTrue(ron_owner.broadcast_cancel(OWNER))
        kind, _ = ron_owner.broadcast_confirm(OWNER)
        self.assertEqual(kind, "error")

    def test_no_chats(self):
        n, _ = ron_owner.broadcast_request("hi", OWNER)
        self.assertEqual(n, 0)

    def test_fanout_sends_to_all(self):
        ron_daemon._broadcast_fanout(["100", "200"], "hi", 1, OWNER, 7)
        bodies = [t for _c, t in self.sent]
        self.assertIn("hi", bodies)
        self.assertTrue(any("2/2" in t for _c, t in self.sent))
        chat_ids = [c for c, t in self.sent if t == "hi"]
        self.assertEqual(sorted(chat_ids), ["100", "200"])

    def test_broadcast_yes_without_pending(self):
        self.owner("broadcast", "/broadcast yes")
        self.assertIn("Nothing pending", self.reply_text())


class QuietTest(PowerBase):
    def setUp(self):
        super().setUp()
        self.orig_maint = ron_daemon.MAINT_FILE
        self.maint = os.path.join(self.tmp, "ron_maintenance.json")
        ron_daemon.MAINT_FILE = self.maint

    def tearDown(self):
        ron_daemon.MAINT_FILE = self.orig_maint
        super().tearDown()

    def test_parse_minutes(self):
        self.assertEqual(ron_owner.parse_quiet_minutes("30"), 30)
        for bad in ("0", "1441", "-5", "abc", "", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    ron_owner.parse_quiet_minutes(bad)

    def test_quiet_sets_expiry(self):
        self.owner("quiet", "/quiet 30")
        self.assertIn("Quiet for 30 min", self.reply_text())
        with open(self.maint) as f:
            data = json.load(f)
        self.assertTrue(data["maintenance"])
        self.assertGreater(data["until"], time.time())
        self.assertTrue(ron_daemon.maintenance_on())

    def test_quiet_expires_automatically(self):
        ron_daemon.set_maintenance(True, OWNER,
                                   until=time.time() - 1)
        self.assertTrue(os.path.exists(self.maint))
        self.assertFalse(ron_daemon.maintenance_on())
        # Auto-lift rewrote the file to maintenance=False.
        with open(self.maint) as f:
            self.assertFalse(json.load(f)["maintenance"])

    def test_manual_maintenance_has_no_expiry(self):
        ron_daemon.set_maintenance(True, OWNER)
        with open(self.maint) as f:
            self.assertNotIn("until", json.load(f))
        self.assertTrue(ron_daemon.maintenance_on())

    def test_quiet_bad_input(self):
        self.owner("quiet", "/quiet")
        self.assertIn("Usage", self.reply_text())


class StatsTest(PowerBase):
    def test_counters_and_card(self):
        ron_owner.note_traffic(1, "A", None, 100, None, "private")
        ron_owner.note_traffic(2, "B", None, 200, "G", "supergroup")
        ron_owner.note_out()
        ron_owner.note_ai_call("groq")
        ron_owner.note_ai_call("groq")
        ron_owner.note_ai_call("gemini")
        snap = ron_owner.today_snapshot()
        self.assertEqual(snap["in"], 2)
        self.assertEqual(snap["out"], 1)
        self.assertEqual(len(snap["chats"]), 2)
        self.assertEqual(snap["ai"], {"groq": 2, "gemini": 1})
        card = ron_owner.stats_text(
            snap, {"hits": 3, "misses": 1}, "1h 2m")
        self.assertIn("in: 2", card)
        self.assertIn("out: 1", card)
        self.assertIn("active chats: 2", card)
        self.assertIn("groq: 2", card)
        self.assertIn("75% hit", card)

    def test_stats_command(self):
        self.owner("stats", "/stats")
        self.assertIn("Today", self.reply_text())

    def test_flush_persists(self):
        ron_owner.note_out()
        ron_owner.flush()
        with open(os.path.join(self.tmp, "ron_stats.json")) as f:
            days = json.load(f)["days"]
        self.assertEqual(len(days), 1)
        day = next(iter(days.values()))
        self.assertEqual(day["out"], 1)


class UsersTest(PowerBase):
    def test_registry_and_card(self):
        ron_owner.note_traffic(42, "Kaviya", "kav", 123, None, "private")
        ron_owner.note_traffic(43, "Soul", None, -100, "Playground",
                               "supergroup")
        roster = ron_owner.user_roster()
        self.assertEqual(len(roster), 2)
        self.assertEqual(roster[0]["name"], "Soul")  # most recent first
        card = ron_owner.users_text(roster)
        self.assertIn("Users (2 total)", card)
        self.assertIn("@kav", card)
        self.assertIn("Playground", card)

    def test_users_command_empty(self):
        self.owner("users", "/users")
        self.assertIn("No users", self.reply_text())

    def test_drop_user(self):
        ron_owner.note_traffic(42, "K", None, 123, None, "private")
        ron_owner.drop_user(42)
        self.assertEqual(ron_owner.user_roster(), [])


class ForgetUserTest(PowerBase):
    class FakeMem:
        def __init__(self):
            self.wiped = []

        def wipe_user(self, uid):
            self.wiped.append(uid)
            return {"messages": 5, "consents": 1, "pendings": 0,
                    "members": 2}

    def test_forget_user_with_mem(self):
        mem = self.FakeMem()
        counts = ron_owner.forget_user(42, mem)
        self.assertEqual(counts["messages"], 5)
        self.assertEqual(mem.wiped, [42])

    def test_forget_user_without_mem(self):
        self.assertIsNone(ron_owner.forget_user(42, None))

    def test_forgetuser_text(self):
        t = ron_owner.forgetuser_text(
            42, {"messages": 5, "facts": 2, "consents": 1, "pendings": 0,
                 "members": 2, "meta": 1}, 9)
        self.assertIn("Forgot user 42", t)
        self.assertIn("5 msgs", t)
        self.assertIn("2 facts", t)
        self.assertIn("history turns purged: 9", t)
        self.assertIn("DM chat is fully wiped", t)

    def test_purge_user_history(self):
        hist_file = os.path.join(self.tmp, "ron_ai_history.json")
        hist = {
            "42": [{"role": "user", "user_id": 42, "content": "hi"},
                   {"role": "assistant", "content": "yo"}],
            "-100": [{"role": "user", "user_id": 42, "content": "hey"},
                     {"role": "assistant", "content": "sup"},
                     {"role": "user", "user_id": 7, "content": "other"},
                     {"role": "assistant", "content": "reply"}],
        }
        with open(hist_file, "w") as f:
            json.dump(hist, f)
        orig_ai = ron_daemon.ron_ai
        import types
        stub = types.SimpleNamespace(
            load_history=lambda: json.load(open(hist_file)),
            save_history=lambda h: json.dump(
                h, open(hist_file, "w")))
        ron_daemon.ron_ai = stub
        try:
            removed = ron_daemon._purge_user_history(42)
        finally:
            ron_daemon.ron_ai = orig_ai
        # DM chat (2 turns) + group user turn + its assistant reply = 4.
        self.assertEqual(removed, 4)
        with open(hist_file) as f:
            rest = json.load(f)
        self.assertNotIn("42", rest)
        self.assertEqual(len(rest["-100"]), 2)
        self.assertEqual(rest["-100"][0]["content"], "other")

    def test_forgetuser_command_flow(self):
        mem = self.FakeMem()
        orig_mem = ron_daemon.mem
        ron_daemon.mem = mem
        hist_file = os.path.join(self.tmp, "ron_ai_history.json")
        with open(hist_file, "w") as f:
            json.dump({}, f)
        import types
        stub = types.SimpleNamespace(
            load_history=lambda: json.load(open(hist_file)),
            save_history=lambda h: json.dump(h, open(hist_file, "w")))
        orig_ai = ron_daemon.ron_ai
        ron_daemon.ron_ai = stub
        try:
            self.owner("forgetuser", "/forgetuser 42")
        finally:
            ron_daemon.mem = orig_mem
            ron_daemon.ron_ai = orig_ai
        self.assertIn("Forgot user 42", self.reply_text())
        self.assertEqual(mem.wiped, [42])

    def test_forgetuser_bad_input(self):
        self.owner("forgetuser", "/forgetuser")
        self.assertIn("Usage", self.reply_text())


class ModelTest(PowerBase):
    def test_apply_pin(self):
        cfg = {"providers": [{"name": "gemini", "models": ["a"]},
                             {"name": "groq", "models": ["b"]},
                             {"name": "openrouter", "models": ["c"]}]}
        out = ron_owner.apply_pin(cfg, "openrouter")
        self.assertEqual([p["name"] for p in out],
                         ["openrouter", "gemini", "groq"])
        # Input not mutated.
        self.assertEqual(cfg["providers"][0]["name"], "gemini")

    def test_apply_pin_rejects(self):
        cfg = {"providers": [{"name": "groq", "models": []}]}
        for bad in ("perplexity", "Muse", "", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    ron_owner.apply_pin(cfg, bad)
        with self.assertRaises(ValueError):
            ron_owner.apply_pin(cfg, "gemini")  # not in chain

    def test_model_text(self):
        t = ron_owner.model_text(
            [{"name": "groq", "models": ["m1", "m2"], "status": "ok",
              "primary": True},
             {"name": "gemini", "models": ["g1"], "status": "cooling (12s)",
              "primary": False}])
        self.assertIn("groq (m1, m2)", t)
        self.assertIn("primary", t)
        self.assertIn("cooling (12s)", t)

    def test_model_pin_writes_config(self):
        import types
        cfg_path = os.path.join(self.tmp, "ron_ai.json")
        cfg = {"providers": [{"name": "gemini", "models": ["a"]},
                             {"name": "groq", "models": ["b"]}]}
        with open(cfg_path, "w") as f:
            json.dump(cfg, f)
        stub = types.SimpleNamespace(
            CONFIG_FILE=cfg_path,
            DEFAULT_PROVIDERS=cfg["providers"],
            load_config=lambda: json.load(open(cfg_path)),
            get_provider_cooldowns=lambda: {})
        orig_ai = ron_daemon.ron_ai
        ron_daemon.ron_ai = stub
        try:
            self.owner("model", "/model groq")
        finally:
            ron_daemon.ron_ai = orig_ai
        self.assertIn("groq \u2192 gemini", self.reply_text())
        with open(cfg_path) as f:
            saved = json.load(f)
        self.assertEqual(saved["providers"][0]["name"], "groq")

    def test_model_show(self):
        import types
        stub = types.SimpleNamespace(
            CONFIG_FILE="/nonexistent",
            DEFAULT_PROVIDERS=[],
            load_config=lambda: {"providers": [
                {"name": "groq", "models": ["m1"]}]},
            get_provider_cooldowns=lambda: {})
        orig_ai = ron_daemon.ron_ai
        ron_daemon.ron_ai = stub
        try:
            self.owner("model", "/model")
        finally:
            ron_daemon.ron_ai = orig_ai
        self.assertIn("Provider chain", self.reply_text())
        self.assertIn("groq", self.reply_text())


class LogsTest(PowerBase):
    def test_tail_log(self):
        p = os.path.join(self.tmp, "test.log")
        with open(p, "w") as f:
            f.write("\n".join(f"line {i}" for i in range(100)))
        lines = ron_owner.tail_log(p, 5)
        self.assertEqual(lines, [f"line {i}" for i in range(95, 100)])
        self.assertEqual(ron_owner.tail_log("/nonexistent/x.log", 5), [])

    def test_logs_text_truncates(self):
        big = ["x" * 100 for _ in range(100)]
        t = ron_owner.logs_text(big)
        self.assertTrue(t.startswith("```"))
        self.assertLessEqual(len(t), 4096)


class OutboundAccountingTest(PowerBase):
    """Every successful outbound path (plain, markup, rich) feeds /stats."""

    def _out_count(self):
        return ron_owner.today_snapshot()["out"]

    def test_send_counts_success_only(self):
        ron_daemon.api = lambda *a, **k: {"ok": True}
        # Call the real send() (not the test stub).
        ron_daemon.send = self.orig_send
        try:
            before = self._out_count()
            ron_daemon.send(1, "hi")
            self.assertEqual(self._out_count(), before + 1)
            ron_daemon.api = lambda *a, **k: {"ok": False}
            ron_daemon.send(1, "hi")
            self.assertEqual(self._out_count(), before + 1)
        finally:
            ron_daemon.send = lambda chat_id, text, **k: self.sent.append(
                (chat_id, text)) or {"ok": True,
                                     "result": {"message_id": 4242}}

    def test_send_markup_counts(self):
        ron_daemon.api = lambda *a, **k: {"ok": True,
                                          "result": {"message_id": 1}}
        before = self._out_count()
        ron_daemon.send_markup(1, "hi", {"inline_keyboard": []})
        self.assertEqual(self._out_count(), before + 1)

    def test_send_rich_counts(self):
        orig = ron_daemon.rich_mod.rich_send
        ron_daemon.rich_mod.rich_send = lambda *a, **k: {
            "ok": True, "result": {"message_id": 1}}
        try:
            before = self._out_count()
            ron_daemon.send_rich(1, "hi")
            self.assertEqual(self._out_count(), before + 1)
        finally:
            ron_daemon.rich_mod.rich_send = orig


class SayRestartTest(PowerBase):
    def test_say(self):
        self.owner("say", "/say -100500 hello there")
        self.assertIn("Sent to -100500", self.reply_text())
        self.assertEqual(self.sent[0][0], -100500)
        self.assertEqual(self.sent[0][1], "hello there")

    def test_say_bad_input(self):
        self.owner("say", "/say")
        self.assertIn("Usage", self.reply_text())
        self.owner("say", "/say abc hi")
        self.assertIn("Usage", self.reply_text())

    def test_restart_delays_flag_until_after_ephemeral(self):
        # The restart flag + shutdown must happen AFTER the 30s ephemeral
        # deletion, not immediately — or the ack would never be deleted.
        import threading as th
        spawned = []
        orig_thread = th.Thread

        class SpyThread(orig_thread):
            def start(self):
                spawned.append(self)
                super().start()

        orig_ttl = ron_owner.EPHEMERAL_TTL
        ron_owner.EPHEMERAL_TTL = 0  # shrink the wait for the test
        th.Thread = SpyThread
        try:
            self.owner("restart", "/restart")
        finally:
            th.Thread = orig_thread
            ron_owner.EPHEMERAL_TTL = orig_ttl
        self.assertIn("Restarting", self.reply_text())
        # Not yet: nothing exits before the ephemeral window.
        self.assertFalse(ron_daemon._SHUTDOWN.is_set())
        self.assertFalse(os.path.exists(ron_owner.restart_flag_path()))
        self.assertEqual(len(spawned), 1)
        spawned[0].join(timeout=10)
        self.assertTrue(os.path.exists(ron_owner.restart_flag_path()))
        self.assertTrue(ron_daemon._SHUTDOWN.is_set())


class RestartFlagTest(unittest.TestCase):
    """The watchdog honors ron_restart.requested without crash counting."""

    def test_clean_restart_flag(self):
        # NOTE: ron_daemon inserts the live dir at sys.path[0], so a plain
        # import would grab the live dir's older ron_watchdog — load the
        # repo's copy by file path instead.
        spec = importlib.util.spec_from_file_location(
            "ron_watchdog_repo", os.path.join(REPO, "ron_watchdog.py"))
        ron_watchdog = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ron_watchdog)
        tmp = tempfile.mkdtemp(prefix="ron_wd_test_")
        # Redirect every watchdog path into the tmp dir.
        orig = {k: getattr(ron_watchdog, k) for k in
                ("DIR", "DAEMON", "DAEMON_LOG", "HEARTBEAT", "CONFIG",
                 "CONFIG_GOOD", "STATE", "WATCH_LOG")}
        for k in ("DAEMON", "DAEMON_LOG", "HEARTBEAT", "CONFIG",
                  "CONFIG_GOOD", "STATE", "WATCH_LOG"):
            setattr(ron_watchdog, k, os.path.join(tmp, k.lower()))
        ron_watchdog.DIR = tmp
        with open(ron_watchdog.CONFIG, "w") as f:
            json.dump({"ok": True}, f)
        with open(os.path.join(tmp, "ron_restart.requested"), "w") as f:
            f.write("123")
        started = []
        orig_start = ron_watchdog.start_daemon
        orig_pids = ron_watchdog.daemon_pids
        ron_watchdog.start_daemon = lambda: started.append(1) or True
        ron_watchdog.daemon_pids = lambda: []
        try:
            ron_watchdog.check_once()
        finally:
            ron_watchdog.start_daemon = orig_start
            ron_watchdog.daemon_pids = orig_pids
            for k, v in orig.items():
                setattr(ron_watchdog, k, v)
        # Flag consumed, daemon (re)started, crash counter untouched.
        self.assertEqual(started, [1])
        self.assertFalse(os.path.exists(
            os.path.join(tmp, "ron_restart.requested")))
        with open(os.path.join(tmp, "state")) as f:
            st = json.load(f)
        self.assertEqual(st["consecutive_crashes"], 0)
        self.assertEqual(st["total_restarts"], 1)


class AiHookTest(unittest.TestCase):
    """Repo ron_ai: AI_CALL_HOOK fires; user_id is tagged on turns but
    stripped before the prompt reaches the provider."""

    def test_hook_and_tagging(self):
        ron_ai = _load_repo_ron_ai()
        self.assertTrue(hasattr(ron_ai, "AI_CALL_HOOK"))
        seen = []
        ron_ai.AI_CALL_HOOK = seen.append
        # Fire the hook the way _request does (can't hit network here;
        # just verify the attribute contract exists and is callable).
        ron_ai.AI_CALL_HOOK("groq")
        self.assertEqual(seen, ["groq"])
        ron_ai.AI_CALL_HOOK = None

        tmp = tempfile.mkdtemp(prefix="ron_ai_test_")
        ron_ai.HISTORY_FILE = os.path.join(tmp, "h.json")
        ron_ai.MEMORY_HOOK = None
        ron_ai._save_turn(111, "hello", "Kaviya", "hi there", user_id=42)
        hist = ron_ai.load_history()
        user_turn = hist["111"][0]
        self.assertEqual(user_turn["user_id"], 42)
        # Prompt building strips the metadata before the API call.
        turns, _summary = ron_ai._prompt_turns(111, "again", "Kaviya")
        for t in turns:
            self.assertEqual(set(t.keys()), {"role", "content"})


if __name__ == "__main__":
    unittest.main()
