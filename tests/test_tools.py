#!/usr/bin/env python3
"""Tool-use layer tests (ron_tools.py — Track B "smarter bot").

Fully offline: every network boundary is faked (fetcher injection,
subprocess runner injection), the reminder store is pointed at a
TemporaryDirectory, and the module never imports ron_ai/ron_daemon
(ron_tools is self-contained by design).

Run: python3 -m unittest discover tests -v  (from ~/workspace/ron-telegram-bot)
"""
import contextlib
import io
from unittest import mock
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)

# NOTE (AGENTS.md): under `unittest discover`, an alphabetically-earlier
# test module may have imported ron_daemon first, which inserts the LIVE
# dir (~/workspace/telegram-bot) at sys.path[0] — pinning the LIVE
# ron_tools in sys.modules. Evict it and re-import the repo copy.
sys.modules.pop("ron_tools", None)
sys.path.insert(0, REPO)
import ron_tools
assert "ron-telegram-bot" in ron_tools.__file__, ron_tools.__file__
from ron_tools import (
    CalendarNotConfigured,
    GwsCliCalendarProvider,
    ReminderStore,
    ToolError,
    calendar_read,
    check_due,
    execute_tool_call,
    format_events,
    get_news,
    get_weather,
    maybe_run_tools,
    parse_tool_calls,
    set_delivery_handler,
    web_search,
    _parse_gws_event,
    _parse_reminder_time,
)

IST = ron_tools._tz("Asia/Calcutta")
FIXED_NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)  # 17:30 IST

PROVIDER_WORDS = ("groq", "gemini", "openrouter", "gpt-oss", "gpt-",
                  "Muse")


def _assert_provider_neutral(test, text):
    for w in PROVIDER_WORDS:
        test.assertNotIn(w, text.lower(),
                         f"user-visible text names a provider/model: {w!r}")


class _FakeFetcher:
    """Scripted URL-keyed fake. Records calls."""

    def __init__(self, routes):
        # routes: list of (url_substring, response_dict_or_exception)
        self.routes = list(routes)
        self.calls = []

    def __call__(self, url, params=None, timeout=10):
        self.calls.append((url, dict(params or {})))
        for sub, resp in self.routes:
            if sub in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected fetch: {url} {params}")


DDG_OK = {
    "Answer": "Pixel 10",
    "AbstractText": "The Pixel 10 is Google's 2025 flagship phone.",
    "RelatedTopics": [
        {"Text": "Pixel 10 Pro has a Tensor G5 chip", "FirstURL": "x"},
        {"Text": "Released August 2025", "FirstURL": "y"},
        {"Name": "group", "Topics": [{"Text": "nested topic"}]},
    ],
}

GEO_OK = {"results": [{"name": "Bengaluru", "country": "India",
                       "latitude": 12.97, "longitude": 77.59}]}
FC_OK = {
    "current": {"temperature_2m": 28.5, "relative_humidity_2m": 70,
                "apparent_temperature": 31.0, "weather_code": 80},
    "daily": {"time": ["2026-09-22", "2026-09-23"],
              "weather_code": [80, 61],
              "temperature_2m_max": [30.1, 29.0],
              "temperature_2m_min": [21.2, 21.0],
              "precipitation_probability_max": [60, 80]},
}

HN_OK = {"hits": [
    {"title": "Show HN: a thing", "points": 120, "num_comments": 45},
    {"title": "Another story", "points": 80, "num_comments": 12},
]}


class FetcherFixture(unittest.TestCase):
    def setUp(self):
        self._old = ron_tools._fetcher

    def tearDown(self):
        ron_tools._fetcher = self._old


# ---------------------------------------------------------------- web_search

class WebSearchTest(FetcherFixture):
    def test_summarizes_without_html(self):
        ron_tools.set_fetcher(_FakeFetcher([("duckduckgo", DDG_OK)]))
        out = web_search("pixel 10 release date")
        self.assertIn("Pixel 10", out)
        self.assertIn("Tensor G5", out)
        self.assertNotIn("<", out)  # no HTML leakage

    def test_empty_results_raise_neutral(self):
        ron_tools.set_fetcher(_FakeFetcher(
            [("duckduckgo", {"RelatedTopics": []})]))
        with self.assertRaises(ToolError) as cm:
            web_search("zzzzqqqq nothing")
        _assert_provider_neutral(self, str(cm.exception))

    def test_network_failure_is_neutral(self):
        ron_tools.set_fetcher(
            _FakeFetcher([("duckduckgo", ConnectionError("dns blew up"))]))
        with self.assertRaises(ToolError) as cm:
            web_search("x")
        _assert_provider_neutral(self, str(cm.exception))

    def test_blank_query_rejected(self):
        with self.assertRaises(ToolError):
            web_search("   ")

    def test_output_truncated(self):
        big = dict(DDG_OK)
        big["RelatedTopics"] = [{"Text": "w" * 2000}]
        ron_tools.set_fetcher(_FakeFetcher([("duckduckgo", big)]))
        self.assertLessEqual(len(web_search("x")),
                             ron_tools.MAX_TOOL_OUTPUT_CHARS + 1)


# ---------------------------------------------------------------- weather

class WeatherTest(FetcherFixture):
    def _fetch(self, geo=GEO_OK, fc=FC_OK):
        ron_tools.set_fetcher(_FakeFetcher(
            [("geocoding-api", geo), ("api.open-meteo", fc)]))

    def test_current_plus_outlook(self):
        self._fetch()
        out = get_weather("Bengaluru")
        self.assertIn("Bengaluru", out)
        self.assertIn("28.5", out)
        self.assertIn("showers", out)   # WMO 80
        self.assertIn("Tomorrow", out)
        self.assertIn("80% rain", out)

    def test_unknown_city(self):
        self._fetch(geo={"results": []})
        with self.assertRaises(ToolError) as cm:
            get_weather("Nowhere XYZ")
        _assert_provider_neutral(self, str(cm.exception))

    def test_wmo_codes_mapped(self):
        self.assertEqual(ron_tools._WMO[95], "thunderstorm")
        self.assertEqual(ron_tools._WMO[0], "clear sky")


# ---------------------------------------------------------------- news

class NewsTest(FetcherFixture):
    def test_front_page(self):
        f = _FakeFetcher([("hn.algolia", HN_OK)])
        ron_tools.set_fetcher(f)
        out = get_news()
        self.assertIn("Show HN: a thing", out)
        self.assertIn("120 pts", out)
        # front_page tag used, no query
        self.assertEqual(f.calls[0][1].get("tags"), "front_page")

    def test_topic_search(self):
        f = _FakeFetcher([("hn.algolia", HN_OK)])
        ron_tools.set_fetcher(f)
        get_news(topic="rust")
        self.assertEqual(f.calls[0][1].get("query"), "rust")

    def test_empty_headlines_neutral(self):
        ron_tools.set_fetcher(_FakeFetcher([("hn.algolia", {"hits": []})]))
        with self.assertRaises(ToolError) as cm:
            get_news()
        _assert_provider_neutral(self, str(cm.exception))


# ---------------------------------------------------------------- reminders

class ReminderStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReminderStore(state_dir=self.tmp.name)
        self._old_handler = ron_tools._delivery_handler

    def tearDown(self):
        ron_tools._delivery_handler = self._old_handler
        self.tmp.cleanup()

    def test_create_relative(self):
        rem = self.store.create(7, 8, "drink water", "in 20 minutes")
        dt = datetime.fromisoformat(rem["remind_at"])
        self.assertAlmostEqual(dt.timestamp(),
                               (datetime.now(timezone.utc)
                                + timedelta(minutes=20)).timestamp(),
                               delta=120)
        self.assertFalse(rem["fired"])
        self.assertEqual(rem["user_id"], 7)
        self.assertEqual(rem["chat_id"], 8)

    def test_create_tomorrow_at(self):
        rem = self.store.create(7, 8, "exam", "tomorrow at 9am")
        dt = datetime.fromisoformat(rem["remind_at"]).astimezone(IST)
        self.assertEqual(dt.hour, 9)
        self.assertEqual(dt.minute, 0)

    def test_create_at_pm_today_or_tomorrow(self):
        # 17:30 IST now; "at 5pm" already passed -> tomorrow 17:00
        rem = self.store.create(7, 8, "call", "at 5pm")
        dt = datetime.fromisoformat(rem["remind_at"]).astimezone(IST)
        self.assertEqual((dt.hour, dt.minute), (17, 0))
        self.assertGreater(dt, FIXED_NOW.astimezone(IST))

    def test_bad_time_rejected_neutral(self):
        with self.assertRaises(ToolError) as cm:
            self.store.create(7, 8, "x", "someday maybe")
        _assert_provider_neutral(self, str(cm.exception))

    def test_blank_text_rejected(self):
        with self.assertRaises(ToolError):
            self.store.create(7, 8, "   ", "in 10 minutes")

    def test_list_sorted_and_cancel(self):
        a = self.store.create(7, 8, "first", "in 10 minutes")
        b = self.store.create(7, 8, "second", "in 5 minutes")
        items = self.store.list(7)
        self.assertEqual([r["id"] for r in items], [b["id"], a["id"]])
        self.assertTrue(self.store.cancel(a["id"], 7))
        self.assertEqual([r["id"] for r in self.store.list(7)], [b["id"]])

    def test_cancel_missing_or_other_user(self):
        r = self.store.create(7, 8, "x", "in 10 minutes")
        with self.assertRaises(ToolError):
            self.store.cancel(999, 7)
        with self.assertRaises(ToolError):
            self.store.cancel(r["id"], 12345)  # other user

    def test_concurrent_creates_get_unique_ids(self):
        # load -> pick id -> save is one atomic step; N threads racing
        # must not mint duplicate reminder ids.
        import threading
        n = 20
        made, errors = [], []

        def worker(i):
            try:
                made.append(self.store.create(7, 8, f"task {i}",
                                              "in 10 minutes"))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        ids = [r["id"] for r in made]
        self.assertEqual(len(ids), n)
        self.assertEqual(len(set(ids)), n)  # no duplicates

    def test_delivery_failure_backs_off(self):
        # A failing handler backs off (5min, 10min...) instead of
        # firing+logging every minute; a success clears the backoff.
        ron_tools._DELIVERY_FAILURES.clear()
        ron_tools._delivery_handler = lambda rem: False
        rem = self.store.create(7, 8, "backoff me", "in 1 minute")
        # Make it due now.
        all_rems = self.store._load()
        for r in all_rems:
            if r["id"] == rem["id"]:
                r["remind_at"] = "2000-01-01T00:00:00+00:00"
        self.store._save(all_rems)

        import time as _t
        with mock.patch.object(ron_tools.time, "time",
                               return_value=1_700_000_000.0):
            self.assertEqual(ron_tools.check_due(store=self.store), 0)
            fails, next_ts = ron_tools._DELIVERY_FAILURES[rem["id"]]
            self.assertEqual(fails, 1)
            self.assertEqual(next_ts, 1_700_000_000.0 + 300)
            # Still in backoff: no second attempt, no extra log spam.
            self.assertEqual(ron_tools.check_due(store=self.store), 0)
            self.assertEqual(
                ron_tools._DELIVERY_FAILURES[rem["id"]][0], 1)
            # After the window, it retries with doubled backoff.
            with mock.patch.object(
                    ron_tools.time, "time",
                    return_value=1_700_000_000.0 + 301):
                self.assertEqual(ron_tools.check_due(store=self.store), 0)
                fails, next_ts = ron_tools._DELIVERY_FAILURES[rem["id"]]
                self.assertEqual(fails, 2)
                self.assertEqual(next_ts, 1_700_000_000.0 + 301 + 600)
        # Success clears the failure record and marks fired.
        ron_tools._delivery_handler = lambda rem: True
        self.assertEqual(ron_tools.check_due(store=self.store), 1)
        self.assertNotIn(rem["id"], ron_tools._DELIVERY_FAILURES)
        ron_tools._DELIVERY_FAILURES.clear()

    def test_persists_across_instances(self):
        r = self.store.create(7, 8, "persist me", "in 10 minutes")
        again = ReminderStore(state_dir=self.tmp.name)
        self.assertEqual([x["id"] for x in again.list(7)], [r["id"]])

    def test_due_and_check_due_fires_once(self):
        sent = []
        set_delivery_handler(lambda rem: sent.append(rem) or True)
        self.store.create(7, 42, "ping", "in 1 minutes")
        n = check_due(now=datetime.now(timezone.utc) + timedelta(minutes=2),
                      store=self.store)
        self.assertEqual(n, 1)
        self.assertEqual(sent[0]["chat_id"], 42)
        self.assertEqual(sent[0]["text"], "ping")
        # fired -> not due again
        n2 = check_due(now=datetime.now(timezone.utc) + timedelta(hours=1),
                       store=self.store)
        self.assertEqual(n2, 0)
        self.assertEqual(len(sent), 1)

    def test_handler_false_keeps_unfired(self):
        set_delivery_handler(lambda rem: False)
        self.store.create(7, 8, "x", "in 1 minutes")
        n = check_due(now=datetime.now(timezone.utc) + timedelta(minutes=5),
                      store=self.store)
        self.assertEqual(n, 0)
        self.assertEqual(len(self.store.due(
            now=datetime.now(timezone.utc) + timedelta(minutes=5))), 1)

    def test_handler_exception_does_not_raise(self):
        def boom(rem):
            raise RuntimeError("send exploded")
        set_delivery_handler(boom)
        self.store.create(7, 8, "x", "in 1 minutes")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            n = check_due(now=datetime.now(timezone.utc)
                          + timedelta(minutes=5), store=self.store)
        self.assertEqual(n, 0)  # exception contained, daemon survives

    def test_no_handler_is_noop(self):
        set_delivery_handler(None)
        self.assertEqual(check_due(store=self.store), 0)

    def test_module_state_dir_is_patchable(self):
        old = ron_tools.STATE_DIR
        ron_tools.STATE_DIR = self.tmp.name
        try:
            s = ReminderStore()  # default path -> patched dir
            r = s.create(7, 8, "via default", "in 10 minutes")
            self.assertTrue(os.path.exists(
                os.path.join(self.tmp.name, "reminders.json")))
            self.assertEqual(r["id"], 1)
        finally:
            ron_tools.STATE_DIR = old


class ReminderTimeParseTest(unittest.TestCase):
    def test_in_hours_and_days(self):
        dt = _parse_reminder_time("in 3 hours", now=FIXED_NOW)
        self.assertEqual(dt, FIXED_NOW + timedelta(hours=3))
        dt = _parse_reminder_time("in 2 days", now=FIXED_NOW)
        self.assertEqual(dt, FIXED_NOW + timedelta(days=2))

    def test_today_at_future(self):
        dt = _parse_reminder_time("today at 6pm", now=FIXED_NOW)
        local = dt.astimezone(IST)
        self.assertEqual((local.day, local.hour), (22, 18))

    def test_iso_passthrough(self):
        dt = _parse_reminder_time("2026-09-23T09:00:00+05:30", now=FIXED_NOW)
        self.assertEqual(dt.isoformat(), "2026-09-23T09:00:00+05:30")

    def test_past_time_rejected(self):
        with self.assertRaises(ToolError):
            _parse_reminder_time("2020-01-01T00:00:00+05:30", now=FIXED_NOW)


# ---------------------------------------------------------------- calendar

class CalendarParseTest(unittest.TestCase):
    def test_all_day_shape(self):
        ev = _parse_gws_event(
            {"all_day": True, "calendar": "varmaadwaith872@gmail.com",
             "start": "2026-09-25", "summary": "Cultural Carnival (Kal-Utsav)",
             "location": "Presidency University"}, "Asia/Calcutta")
        self.assertTrue(ev.all_day)
        self.assertEqual(ev.title, "Cultural Carnival (Kal-Utsav)")
        self.assertEqual(ev.location, "Presidency University")
        self.assertEqual(ev.start.strftime("%Y-%m-%d"), "2026-09-25")

    def test_timed_shape(self):
        ev = _parse_gws_event(
            {"summary": "DCCN Lab",
             "event_starts_at": {"user_local": "2026-09-22T14:00:00+05:30"},
             "event_ends_at": {"user_local": "2026-09-22T16:00:00+05:30"}},
            "Asia/Calcutta")
        self.assertFalse(ev.all_day)
        self.assertEqual(ev.start.hour, 14)
        self.assertEqual(ev.end.hour, 16)

    def test_format_empty(self):
        self.assertIn("Nothing on your calendar today",
                      format_events([], "today"))

    def test_format_mixed(self):
        evs = [
            _parse_gws_event(
                {"all_day": True, "start": "2026-09-25",
                 "summary": "Kal-Utsav", "location": "Presidency University"},
                "Asia/Calcutta"),
            _parse_gws_event(
                {"summary": "DCCN Lab",
                 "event_starts_at": {"user_local": "2026-09-22T14:00:00+05:30"},
                 "event_ends_at": {"user_local": "2026-09-22T16:00:00+05:30"}},
                "Asia/Calcutta"),
        ]
        out = format_events(evs, "week")
        self.assertIn("Kal-Utsav", out)
        self.assertIn("all day", out)
        self.assertIn("2:00 PM", out)
        self.assertIn("Presidency University", out)
        _assert_provider_neutral(self, out)


class GwsCliProviderTest(unittest.TestCase):
    def _runner(self, script):
        calls = []

        def run(argv):
            calls.append(argv)
            return script(argv)
        run.calls = calls
        return run

    def test_not_connected_raises_with_help(self):
        runner = self._runner(lambda argv: {"status": "not_connected"})
        prov = GwsCliCalendarProvider(runner=runner)
        with self.assertRaises(CalendarNotConfigured) as cm:
            prov.agenda("today")
        self.assertIn("Connectors", str(cm.exception))
        _assert_provider_neutral(self, str(cm.exception))

    def test_runner_error_maps_to_help(self):
        def script(argv):
            if argv[1] == "status":
                return {"status": "connected"}
            raise RuntimeError("auth expired")
        prov = GwsCliCalendarProvider(runner=self._runner(script))
        with self.assertRaises(CalendarNotConfigured) as cm:
            prov.agenda("today")
        _assert_provider_neutral(self, str(cm.exception))

    def test_agenda_parses_and_sorts(self):
        def script(argv):
            if argv[1] == "status":
                return {"status": "connected"}
            return {"events": [
                {"summary": "Late",
                 "event_starts_at": {"user_local": "2026-09-22T18:00:00+05:30"}},
                {"summary": "Early",
                 "event_starts_at": {"user_local": "2026-09-22T09:00:00+05:30"}},
                {"summary": "Broken", "event_starts_at": {}},
            ]}
        prov = GwsCliCalendarProvider(runner=self._runner(script))
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):  # skip-log goes to stderr
            evs = prov.agenda("today")
        self.assertEqual([e.title for e in evs], ["Early", "Late"])

    def test_calendar_read_uses_provider(self):
        class FakeProv:
            def agenda(self, when="today"):
                return []
        self.assertIn("Nothing on your calendar",
                      calendar_read("tomorrow", provider=FakeProv()))


# ---------------------------------------------------------------- tool-call parsing

class ParseToolCallsTest(unittest.TestCase):
    BLOCK = ('```tool\n{"name": "web_search", "arguments": '
             '{"query": "x"}}\n```')

    def test_single(self):
        calls, notes = parse_tool_calls(self.BLOCK)
        self.assertEqual(calls, [("web_search", {"query": "x"})])
        self.assertEqual(notes, [])

    def test_multiple_and_cap(self):
        text = self.BLOCK * 5
        calls, notes = parse_tool_calls(text)
        self.assertEqual(len(calls), 3)
        self.assertTrue(any("limit" in n for n in notes))

    def test_malformed_and_unknown_ignored(self):
        text = ("```tool\nnot json\n```\n"
                "```tool\n{\"name\": \"nope\", \"arguments\": {}}\n```\n"
                "```tool\n{\"name\": \"news\", \"arguments\": [1]}\n```\n"
                + self.BLOCK)
        calls, notes = parse_tool_calls(text)
        self.assertEqual(calls, [("web_search", {"query": "x"})])
        self.assertEqual(len(notes), 3)

    def test_none_text_yields_nothing(self):
        self.assertEqual(parse_tool_calls("NONE"), ([], []))
        self.assertEqual(parse_tool_calls(""), ([], []))


# ---------------------------------------------------------------- execute_tool_call

class ExecuteToolCallTest(FetcherFixture):
    def test_read_only_runs(self):
        ron_tools.set_fetcher(_FakeFetcher([("duckduckgo", DDG_OK)]))
        out = execute_tool_call("web_search", {"query": "x"})
        self.assertIn("Pixel 10", out)

    def test_unknown_tool(self):
        out = execute_tool_call("teleport", {})
        self.assertIn("not available", out)

    def test_acting_refused_without_flag(self):
        out = execute_tool_call("create_reminder",
                                {"text": "x", "when": "in 10 minutes"})
        self.assertIn("explicit request", out)

    def test_tool_crash_is_neutral_and_contained(self):
        # A fetcher blow-up surfaces as a neutral service error, and a
        # non-ToolError bug inside a tool is contained, never raised.
        ron_tools.set_fetcher(_FakeFetcher(
            [("duckduckgo", RuntimeError("weird"))]))
        out = execute_tool_call("web_search", {"query": "x"})
        self.assertIn("couldn't reach", out)
        _assert_provider_neutral(self, out)
        ron_tools.set_fetcher(_FakeFetcher([("duckduckgo", DDG_OK)]))
        with mock.patch.object(ron_tools, "web_search",
                                        side_effect=ValueError("bug")):
            out2 = execute_tool_call("web_search", {"query": "x"})
        self.assertIn("snag", out2)
        _assert_provider_neutral(self, out2)


# ---------------------------------------------------------------- maybe_run_tools (the agentic loop)

def _tool_block(name, args):
    return ("```tool\n" + json.dumps({"name": name, "arguments": args})
            + "\n```")


class MaybeRunToolsTest(FetcherFixture):
    MSGS = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "what's the weather in Bengaluru?"}]

    def test_short_text_still_probes(self):
        # No one-word guard: pure acks ("ok", "thanks") are intercepted by
        # the daemon's zero-cost canned replies BEFORE the AI path, so
        # maybe_run_tools always probes. A bare "weather" must reach the
        # probe (it is exactly what the tools exist for).
        calls = []
        out = maybe_run_tools(self.MSGS, lambda m: calls.append(m) or "NONE",
                              user_text="ok")
        self.assertIsNone(out)
        self.assertEqual(len(calls), 1)  # probed, model said NONE
        calls.clear()
        out = maybe_run_tools(self.MSGS, lambda m: calls.append(m) or "NONE",
                              user_text="weather")
        self.assertIsNone(out)
        self.assertEqual(len(calls), 1)  # probed, not skipped

    def test_none_reply_returns_none(self):
        out = maybe_run_tools(self.MSGS, lambda m: "NONE",
                              user_text="weather in Bengaluru today please")
        self.assertIsNone(out)

    def test_probe_failure_returns_none(self):
        def boom(m):
            raise RuntimeError("provider down")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            out = maybe_run_tools(self.MSGS, boom,
                                  user_text="weather in Bengaluru today")
        self.assertIsNone(out)

    def test_single_tool_executes_and_returns_block(self):
        ron_tools.set_fetcher(_FakeFetcher(
            [("geocoding-api", GEO_OK), ("api.open-meteo", FC_OK)]))
        probe = _tool_block("weather", {"city": "Bengaluru"})
        seen = []

        def fake_complete(msgs):
            seen.append(msgs)
            return probe if len(seen) == 1 else "NONE"

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            out = maybe_run_tools(
                self.MSGS, fake_complete,
                user_text="what is the weather in Bengaluru today")
        self.assertIsNotNone(out)
        self.assertIn("Bengaluru", out)
        self.assertIn("28.5", out)
        self.assertIn("never mention tools", out)
        _assert_provider_neutral(self, out)

    def test_two_pass_chaining(self):
        ron_tools.set_fetcher(_FakeFetcher(
            [("duckduckgo", DDG_OK),
             ("geocoding-api", GEO_OK), ("api.open-meteo", FC_OK)]))
        replies = [_tool_block("web_search", {"query": "capital of France"}),
                   _tool_block("weather", {"city": "Paris"}),
                   "NONE"]
        out = maybe_run_tools(self.MSGS, lambda m: replies.pop(0),
                              user_text="weather in the capital of France",
                              max_passes=2)
        self.assertIsNotNone(out)
        self.assertIn("web_search", out)
        self.assertIn("weather", out)

    def test_max_passes_one_means_single_probe(self):
        calls = []
        ron_tools.set_fetcher(_FakeFetcher([("duckduckgo", DDG_OK)]))

        def fake_complete(msgs):
            calls.append(msgs)
            return _tool_block("web_search", {"query": "x"})

        maybe_run_tools(self.MSGS, fake_complete,
                        user_text="search for something please",
                        max_passes=1)
        self.assertEqual(len(calls), 1)

    def test_read_only_probe_hides_acting_tools(self):
        seen = []
        ron_tools.set_fetcher(_FakeFetcher([("duckduckgo", DDG_OK)]))

        def fake_complete(msgs):
            seen.append(msgs)
            return "NONE"

        maybe_run_tools(self.MSGS, fake_complete,
                        user_text="what is the weather like today")
        probe_text = json.dumps(seen[0])
        self.assertNotIn("create_reminder", probe_text)

    def test_full_catalog_when_explicit_reminder(self):
        seen = []

        def fake_complete(msgs):
            seen.append(msgs)
            return "NONE"

        maybe_run_tools(self.MSGS, fake_complete,
                        user_text="remind me to call mom in 20 minutes",
                        allow_acting=True)
        self.assertIn("create_reminder", json.dumps(seen[0]))

    def test_create_reminder_end_to_end(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ctx = {"user_id": 7, "chat_id": 42,
               "reminder_store": ReminderStore(state_dir=tmp.name)}
        probe = _tool_block("create_reminder",
                            {"text": "call mom", "when": "in 20 minutes"})
        replies = [probe, "NONE"]
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            out = maybe_run_tools(
                self.MSGS, lambda m: replies.pop(0),
                user_text="remind me to call mom in 20 minutes",
                allow_acting=True, ctx=ctx)
        self.assertIsNotNone(out)
        self.assertIn("Reminder set", out)
        self.assertEqual(len(ctx["reminder_store"].list(7)), 1)

    def test_acting_refused_without_allow_acting(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ctx = {"user_id": 7, "chat_id": 42,
               "reminder_store": ReminderStore(state_dir=tmp.name)}
        probe = _tool_block("create_reminder",
                            {"text": "call mom", "when": "in 20 minutes"})
        replies = [probe, "NONE"]
        out = maybe_run_tools(self.MSGS, lambda m: replies.pop(0),
                              user_text="remind me to call mom in 20 minutes",
                              allow_acting=False, ctx=ctx)
        self.assertIsNotNone(out)  # block returned, but call refused inside
        self.assertIn("explicit request", out)
        self.assertEqual(ctx["reminder_store"].list(7), [])

    def test_on_tool_callback(self):
        ron_tools.set_fetcher(_FakeFetcher([("duckduckgo", DDG_OK)]))
        fired = []
        replies = [_tool_block("web_search", {"query": "x"}), "NONE"]
        maybe_run_tools(self.MSGS, lambda m: replies.pop(0),
                        user_text="search the web for x please",
                        on_tool=lambda n, a: fired.append(n))
        self.assertEqual(fired, ["web_search"])

    def test_results_block_truncated(self):
        big = dict(DDG_OK)
        big["AbstractText"] = "z" * 5000
        ron_tools.set_fetcher(_FakeFetcher([("duckduckgo", big)]))
        replies = [_tool_block("web_search", {"query": "x"})] * 4
        out = maybe_run_tools(self.MSGS, lambda m: replies.pop(0),
                              user_text="search the web for x please now",
                              max_passes=2)
        self.assertLessEqual(len(out), ron_tools.MAX_RESULTS_CHARS + 1)


# ---------------------------------------------------------------- registry / schemas

class RegistryTest(unittest.TestCase):
    def test_seven_tools_registered(self):
        names = [t["name"] for t in ron_tools.TOOL_SCHEMAS]
        self.assertEqual(names,
                         ["web_search", "weather", "news", "calendar_read",
                          "create_reminder", "list_reminders",
                          "cancel_reminder"])

    def test_read_only_flags(self):
        ro = {t["name"] for t in ron_tools.TOOL_SCHEMAS if t["read_only"]}
        self.assertEqual(ro, {"web_search", "weather", "news",
                              "calendar_read"})
        self.assertEqual(set(ron_tools.READ_ONLY_TOOLS), ro)
        self.assertEqual(set(ron_tools.ACTING_TOOLS),
                         {"create_reminder", "list_reminders",
                          "cancel_reminder"})

    def test_catalog_mentions_all_tools(self):
        for t in ron_tools.TOOL_SCHEMAS:
            self.assertIn(t["name"], ron_tools.TOOL_CATALOG_TEXT)

    def test_reminder_request_regex(self):
        for s in ("remind me in 10 minutes", "set a reminder for 5pm",
                  "ping me tomorrow", "don't let me forget the keys"):
            self.assertTrue(ron_tools.REMINDER_REQUEST_RE.search(s), s)
        self.assertFalse(ron_tools.REMINDER_REQUEST_RE.search(
            "what is the weather"))


if __name__ == "__main__":
    unittest.main()
