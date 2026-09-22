#!/usr/bin/env python3
"""Ron tool-use layer — the "smarter bot" upgrade (Track B).

SELF-CONTAINED: this module imports nothing from ron_ai or ron_daemon.
ron_ai.py does sys.path surgery that points at the LIVE deploy dir, so
importing it here would poison tests and risk touching live state. The
brain loop calls INTO this module through the integration contract below.

==================== INTEGRATION CONTRACT ====================
(for the ron_ai.py integrator — Track B does not edit ron_ai.py)

Entry point:

    result_text = ron_tools.maybe_run_tools(
        messages, complete_fn,
        user_text="...",          # raw user message (explicit-request gating)
        allow_acting=False,       # set True only on explicit reminder asks
        on_tool=None,             # optional callback(name, args) per execution
    )

* `messages` — the OpenAI-style message list the integrator already built
  for this turn (system + history), exactly as it would send to the model.
* `complete_fn(messages) -> str` — a NON-streaming completion callback the
  integrator wires to its own provider chain. Contract:
    - takes the message list, returns the model's full text reply, stripped
    - raises on failure; maybe_run_tools treats ANY exception from it as
      "no tool help this turn" and returns None (graceful degradation)
    - what it surfaces must stay provider-neutral; tool code never names
      providers or models in user-visible text, and complete_fn should not
      either
* Returns a plain-text tool-results block, or None when the model asked
  for no tools / the tool pass failed. On a non-None return the integrator
  appends it to `messages` as ONE new message:

      {"role": "user", "content": result_text}

  and then proceeds with its NORMAL (streaming) reply call. The final
  answer is therefore the model's own words, grounded in tool data, and
  streams exactly as today. On None, the integrator proceeds unchanged.

Loop semantics:
* At most MAX_PASSES (2) probe passes; at most MAX_CALLS_PER_PASS (3)
  tool executions per pass. Small on purpose: tools are a spice, not the
  meal, and each pass costs a full model call.
* Each tool HTTP call is bounded by TOOL_HTTP_TIMEOUT (10s); a whole pass
  is bounded by PASS_TIMEOUT (30s). On timeout, return whatever results
  were gathered (or None if none) — never raise into the chat turn.
* Read-only tools (web_search, weather, news, calendar_read) run
  automatically when the model requests them.
* Acting tools (create_reminder) run ONLY when allow_acting=True. The
  integrator sets that flag when the user's message is an explicit
  reminder request (REMINDER_REQUEST_RE is provided as the reference
  heuristic), or when a prior turn confirmed creation. The tool catalog
  text ALSO tells the model to confirm before creating — two gates.
* Recommended call site in chat_stream()/chat(): right after `messages`
  is built, before the streaming _request. Skip the tool pass entirely
  for cheap/small-talk turns (nothing worth looking up there) and for
  secretary-mode business replies unless the other person asked.

How the model asks (canonical text protocol — works on EVERY provider,
including ones without native tool support; this is the deliberate
degradation path):

    The probe pass sends build_probe_messages(messages): the turn so far
    plus a system instruction containing TOOL_CATALOG_TEXT, asking the
    model to reply with tool calls or the word NONE. A tool call is a
    fenced block:

        ```tool
        {"name": "web_search", "arguments": {"query": "pixel 10 release date"}}
        ```

    parse_tool_calls() extracts and validates them (unknown names and bad
    args are skipped with a note, never executed). Multiple blocks in one
    reply are fine, up to MAX_CALLS_PER_PASS.

Native function calling (optional upgrade path):
    TOOL_SCHEMAS holds OpenAI-style function schemas for every tool, for
    integrators on providers that support native tool_calls (gpt-oss on
    Groq does). If the integrator goes native, it should STILL route
    execution through execute_tool_call(name, arguments, ...) so gating,
    truncation, and provider-neutral errors stay identical. The text
    protocol remains the fallback for providers without tool support.

Reminders — delivery interface (daemon wiring, NOT done here):
    The daemon (ron_daemon.py) owns delivery. It must:
      1. ron_tools.set_delivery_handler(fn) once at startup, where
         fn(reminder_dict) -> bool sends reminder_dict["text"] to
         reminder_dict["chat_id"] (via the daemon's normal send path)
         and returns True on success.
      2. Call ron_tools.check_due() about once a minute (its existing
         loop/timer). check_due() fires every due, unfired reminder
         through the handler and marks it fired. Returns the count fired.
    Reminder dict shape: {"id", "user_id", "chat_id", "text",
    "remind_at" (ISO-8601), "created_at" (ISO), "fired" (bool),
    "repeat" (None for now)}.
    The store lives at <STATE_DIR>/reminders.json. STATE_DIR defaults to
    the bot state dir (~/workspace/telegram-bot) and is a plain module
    attribute so tests/deployments can repoint it: ron_tools.STATE_DIR.

Token discipline: every tool output is truncated to MAX_TOOL_OUTPUT_CHARS
(800); the whole results block to MAX_RESULTS_CHARS (2200). Tool traffic
never names providers/models in user-visible text.

Network: all HTTP goes through _fetch_json(), which honors an injected
fetcher (set_fetcher) — tests inject fakes; production uses urllib with
a short timeout and a bot User-Agent.
====================================================================
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HOME = os.path.expanduser("~")
# Bot state dir. Plain module attribute on purpose: tests repoint it at a
# TemporaryDirectory; a deployment could too. Default = the live bot dir.
STATE_DIR = os.path.join(HOME, "workspace", "telegram-bot")
REMINDERS_FILE = "reminders.json"

MAX_PASSES = 2            # probe passes per turn, max
MAX_CALLS_PER_PASS = 3    # tool executions per pass, max
TOOL_HTTP_TIMEOUT = 10    # seconds per tool HTTP call
PASS_TIMEOUT = 30         # seconds per whole tool pass
MAX_TOOL_OUTPUT_CHARS = 800    # per-tool truncation (token discipline)
MAX_RESULTS_CHARS = 2200       # whole results-block truncation
USER_TZ = "Asia/Calcutta"      # Adwaith's timezone (USER.md)

# Read-only tools run automatically; acting tools need allow_acting=True.
READ_ONLY_TOOLS = ("web_search", "weather", "news", "calendar_read")
ACTING_TOOLS = ("create_reminder", "list_reminders", "cancel_reminder")
# Reference heuristic for the integrator: when the user's raw text looks
# like an explicit reminder request, it may set allow_acting=True.
REMINDER_REQUEST_RE = re.compile(
    r"\b(remind me|reminder|remind\b.*\b(in|at|tomorrow|today)|"
    r"set (an? |the )?reminder|ping me|nudge me|don't let me forget|"
    r"dont let me forget)\b",
    re.IGNORECASE,
)

_log = lambda *a: print("[ron_tools]", *a, file=sys.stderr)  # noqa: E731

def _tz(name):
    """ZoneInfo with a fixed-offset fallback (this box has no tzdata).

    Asia/Calcutta has no DST, so UTC+5:30 is exact, not approximate.
    """
    try:
        return ZoneInfo(name)
    except Exception:
        if name == "Asia/Calcutta":
            return timezone(timedelta(hours=5, minutes=30))
        return timezone.utc




# ---------------------------------------------------------------------------
# Errors — always provider-neutral in user-visible text
# ---------------------------------------------------------------------------

class ToolError(Exception):
    """A tool failed in a way the user may see. Message must be plain
    language, never naming providers, models, APIs, or keys."""


class CalendarNotConfigured(ToolError):
    """Calendar reads need a connection the user hasn't granted (or that
    isn't visible from the daemon's environment). The message tells them
    exactly what to do."""


# ---------------------------------------------------------------------------
# HTTP plumbing — injectable fetcher so tests never touch the network
# ---------------------------------------------------------------------------

_fetcher = None  # fn(url, params=None, timeout=10) -> parsed JSON (dict)


def set_fetcher(fn):
    """Inject a fake fetcher (tests). fn(url, params=None, timeout) -> dict."""
    global _fetcher
    _fetcher = fn


def reset_fetcher():
    global _fetcher
    _fetcher = None


def _fetch_json(url, params=None, timeout=TOOL_HTTP_TIMEOUT):
    if _fetcher is not None:
        try:
            return _fetcher(url, params=params, timeout=timeout)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError("I couldn't reach that service just now \u2014 "
                            "try again in a bit.") from e
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"User-Agent": "ron-telegram-bot/1.0 (+telegram)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        raise ToolError("I couldn't reach that service just now — "
                        "try again in a bit.") from e


def _truncate(text, limit=MAX_TOOL_OUTPUT_CHARS):
    text = (text or "").strip()
    if len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


# ---------------------------------------------------------------------------
# Tool 1: web_search — DuckDuckGo Instant Answer API (free, no key)
# ---------------------------------------------------------------------------

def web_search(query, count=5):
    """Short summarized web answers. Never returns raw HTML."""
    query = (query or "").strip()
    if not query:
        raise ToolError("I need something to search for.")
    data = _fetch_json("https://api.duckduckgo.com/",
                       {"q": query, "format": "json", "no_html": "1",
                        "skip_disambig": "1"})
    lines = []
    answer = (data.get("Answer") or "").strip()
    abstract = (data.get("AbstractText") or "").strip()
    if answer:
        lines.append("Answer: " + answer)
    if abstract:
        lines.append(abstract)
    for topic in (data.get("RelatedTopics") or [])[:count]:
        if isinstance(topic, dict) and topic.get("Text"):
            lines.append("• " + topic["Text"].strip())
        elif isinstance(topic, dict):  # grouped topics ("Name"/"Topics")
            for sub in (topic.get("Topics") or [])[:2]:
                if sub.get("Text"):
                    lines.append("• " + sub["Text"].strip())
    if not lines:
        raise ToolError(f"No good results for '{query}' — try different words.")
    return _truncate("\n".join(lines))


# ---------------------------------------------------------------------------
# Tool 2: weather — Open-Meteo (free, no key)
# ---------------------------------------------------------------------------

_WMO = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "icy fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    95: "thunderstorm", 96: "storm with hail", 99: "storm with hail",
}


def get_weather(city, days=2):
    """City -> current conditions + brief outlook. No API key."""
    city = (city or "").strip()
    if not city:
        raise ToolError("Which city should I check?")
    geo = _fetch_json("https://geocoding-api.open-meteo.com/v1/search",
                      {"name": city, "count": 1, "language": "en",
                       "format": "json"})
    results = geo.get("results") or []
    if not results:
        raise ToolError(f"Couldn't find '{city}' — check the spelling?")
    g = results[0]
    fc = _fetch_json(
        "https://api.open-meteo.com/v1/forecast",
        {"latitude": g["latitude"], "longitude": g["longitude"],
         "current": "temperature_2m,relative_humidity_2m,"
                    "apparent_temperature,weather_code",
         "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                  "precipitation_probability_max",
         "timezone": "auto", "forecast_days": max(1, min(days, 7))})
    cur = fc.get("current") or {}
    daily = fc.get("daily") or {}
    place = ", ".join(x for x in (g.get("name"), g.get("country"))
                      if x)
    desc = _WMO.get(cur.get("weather_code"), "—")
    out = [f"{place}: {cur.get('temperature_2m')}°C, {desc} "
           f"(feels {cur.get('apparent_temperature')}°C, "
           f"humidity {cur.get('relative_humidity_2m')}%)"]
    ddates = daily.get("time") or []
    for i in range(1, min(len(ddates), max(1, min(days, 7)))):
        ddesc = _WMO.get((daily.get("weather_code") or [None])[i], "—")
        mx = (daily.get("temperature_2m_max") or ["?"])[i]
        mn = (daily.get("temperature_2m_min") or ["?"])[i]
        pp = (daily.get("precipitation_probability_max") or [None])[i]
        label = "Tomorrow" if i == 1 else ddates[i]
        rain = f", {pp}% rain" if pp is not None else ""
        out.append(f"{label}: {ddesc}, {mn}–{mx}°C{rain}")
    return _truncate("\n".join(out))


# ---------------------------------------------------------------------------
# Tool 3: news — Hacker News front page via Algolia (free, no key)
# ---------------------------------------------------------------------------

def get_news(topic=None, count=6):
    """Tech headlines. No API key."""
    count = max(1, min(count or 6, 10))
    if topic and topic.strip():
        data = _fetch_json(
            "https://hn.algolia.com/api/v1/search",
            {"query": topic.strip(), "tags": "story",
             "hitsPerPage": count})
    else:
        data = _fetch_json("https://hn.algolia.com/api/v1/search",
                           {"tags": "front_page", "hitsPerPage": count})
    hits = data.get("hits") or []
    if not hits:
        raise ToolError("No headlines right now — try again in a bit.")
    lines = []
    for h in hits[:count]:
        title = (h.get("title") or "").strip()
        pts = h.get("points")
        comments = h.get("num_comments")
        meta = " / ".join(x for x in (
            f"{pts} pts" if pts is not None else "",
            f"{comments} comments" if comments is not None else "") if x)
        lines.append(f"• {title}" + (f" ({meta})" if meta else ""))
    return _truncate("\n".join(lines))


# ---------------------------------------------------------------------------
# Tool 4: reminders — local persistent store + daemon delivery interface
# ---------------------------------------------------------------------------

def _state_path(name):
    d = STATE_DIR
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


def _now_utc():
    return datetime.now(timezone.utc)


def _parse_reminder_time(text, now=None, tz_name=USER_TZ):
    """Parse common reminder phrasings -> aware datetime.

    Handles: "in 10 minutes/hours", "in 2 days", "at 5pm", "at 17:30",
    "tomorrow at 9am", "today at 6:30pm", "on 2026-09-25 at 9am",
    ISO-8601. Raises ToolError (provider-neutral) when unparseable or
    in the past.
    """
    tz = _tz(tz_name)
    now = now or _now_utc()
    local_now = now.astimezone(tz)
    text = (text or "").strip().lower()

    m = re.match(r"in\s+(\d+)\s*(minute|min|hour|hr|day|week)s?\b", text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"minute": timedelta(minutes=n), "min": timedelta(minutes=n),
                 "hour": timedelta(hours=n), "hr": timedelta(hours=n),
                 "day": timedelta(days=n),
                 "week": timedelta(weeks=n)}[unit]
        return now + delta

    m = re.match(
        r"(on\s+(\d{4}-\d{2}-\d{2})\s+)?(today|tomorrow)?\s*at\s+"
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", text)
    if m:
        day = local_now.date()
        if m.group(2):
            day = datetime.strptime(m.group(2), "%Y-%m-%d").date()
        elif m.group(3) == "tomorrow":
            day += timedelta(days=1)
        hour, minute = int(m.group(4)), int(m.group(5) or 0)
        ampm = m.group(6)
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ToolError("That time doesn't look right — try e.g. "
                            "'at 5pm' or 'tomorrow at 9am'.")
        dt = datetime(day.year, day.month, day.day, hour, minute,
                      tzinfo=tz)
        # Bare "at 5pm" that already passed today -> assume tomorrow.
        if not m.group(2) and not m.group(3) and dt <= local_now:
            dt += timedelta(days=1)
        if dt <= local_now:
            raise ToolError("That time is in the past — pick a future time.")
        return dt

    # ISO-8601 last resort.
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        if dt <= now:
            raise ToolError("That time is in the past — pick a future time.")
        return dt
    except (ValueError, TypeError):
        pass
    raise ToolError("I couldn't understand that time — try 'in 20 minutes', "
                    "'at 5pm', or 'tomorrow at 9am'.")


class ReminderStore:
    """JSON-backed reminder store. Point STATE_DIR at a temp dir in tests."""

    def __init__(self, state_dir=None):
        self.path = os.path.join(state_dir or STATE_DIR, REMINDERS_FILE)

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save(self, items):
        d = os.path.dirname(self.path)
        os.makedirs(d, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=1)
        os.replace(tmp, self.path)

    def create(self, user_id, chat_id, text, when, tz_name=USER_TZ):
        """Create a reminder. `when` is natural language ('in 10 minutes')
        or ISO-8601. Returns the reminder dict."""
        text = (text or "").strip()
        if not text:
            raise ToolError("What should I remind you about?")
        dt = _parse_reminder_time(when, tz_name=tz_name)
        items = self._load()
        rid = max([r.get("id", 0) for r in items] + [0]) + 1
        rem = {"id": rid, "user_id": user_id, "chat_id": chat_id,
               "text": text, "remind_at": dt.isoformat(),
               "created_at": _now_utc().isoformat(), "fired": False,
               "repeat": None}
        items.append(rem)
        self._save(items)
        _log(f"reminder #{rid} created for {dt.isoformat()}")
        return rem

    def list(self, user_id):
        items = [r for r in self._load()
                 if r.get("user_id") == user_id and not r.get("fired")]
        items.sort(key=lambda r: r.get("remind_at", ""))
        return items

    def cancel(self, reminder_id, user_id):
        items = self._load()
        for r in items:
            if r.get("id") == reminder_id and r.get("user_id") == user_id:
                if r.get("fired"):
                    raise ToolError("That reminder already went off.")
                items.remove(r)
                self._save(items)
                _log(f"reminder #{reminder_id} cancelled")
                return True
        raise ToolError("Couldn't find that reminder — it may have "
                        "already fired or been cancelled.")

    def due(self, now=None):
        now = now or _now_utc()
        return [r for r in self._load()
                if not r.get("fired") and r.get("remind_at")
                and datetime.fromisoformat(r["remind_at"]) <= now]

    def mark_fired(self, reminder_id):
        items = self._load()
        for r in items:
            if r.get("id") == reminder_id:
                r["fired"] = True
        self._save(items)


# Delivery hook interface (daemon-owned; see module docstring).
_delivery_handler = None  # fn(reminder_dict) -> bool


def set_delivery_handler(fn):
    """Daemon startup: fn(reminder) -> bool. Must send reminder['text']
    to reminder['chat_id'] via the daemon's normal send path."""
    global _delivery_handler
    _delivery_handler = fn


def check_due(now=None, store=None):
    """Fire every due reminder through the delivery handler.

    The daemon calls this ~once a minute. Returns the number fired.
    Never raises into the daemon loop: handler exceptions are logged and
    the reminder stays unfired for the next pass."""
    handler = _delivery_handler
    if handler is None:
        return 0
    store = store or ReminderStore()
    fired = 0
    for rem in store.due(now=now):
        try:
            ok = handler(dict(rem))
        except Exception as e:  # noqa: BLE001 — daemon must not die here
            _log(f"delivery handler failed for reminder #{rem.get('id')}: {e}")
            continue
        if ok:
            store.mark_fired(rem["id"])
            fired += 1
        else:
            _log(f"delivery handler returned False for reminder "
                 f"#{rem.get('id')}")
    return fired


def _fmt_reminder_list(items, tz_name=USER_TZ):
    if not items:
        return "No reminders set."
    tz = _tz(tz_name)
    lines = []
    for r in items:
        dt = datetime.fromisoformat(r["remind_at"]).astimezone(tz)
        lines.append(f"#{r['id']} — {r['text']} "
                     f"({dt.strftime('%a %d %b, %I:%M %p')})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 5: calendar reads — plumbing + parsing + formatting.
#
# CREDENTIAL BOUNDARY: reads go through the Google Workspace connector CLI
# (hatch_gws_cli), whose OAuth is granted by Adwaith in the Muse app — no
# API keys or secrets live in this module. If the CLI is missing or reports
# not-connected, calendar_read raises CalendarNotConfigured with exact
# user-facing next steps (see CALENDAR_CONNECT_HELP). Nothing is stalled:
# the provider interface, parsing, and formatting below are complete and
# test-covered; the daemon only needs a connected CLI at runtime.
# ---------------------------------------------------------------------------

CALENDAR_CONNECT_HELP = (
    "I can't see your calendar right now. To fix it: open the Muse app, "
    "go to Settings → Connectors → Google Calendar, and connect your "
    "Google account, then ask me again.")


@dataclass
class CalendarEvent:
    title: str
    start: datetime                      # aware
    end: datetime | None = None          # aware; None for all-day
    all_day: bool = False
    location: str = ""
    calendar: str = ""


class CalendarProvider:
    """Interface every calendar backend implements (read-only)."""

    def agenda(self, when="today"):
        """Return [CalendarEvent] for when in {"today","tomorrow","week"}."""
        raise NotImplementedError

    @staticmethod
    def from_config(cfg):
        kind = (cfg or {}).get("kind", "gws_cli")
        if kind == "gws_cli":
            return GwsCliCalendarProvider(
                account=(cfg or {}).get("account"),
                tz_name=(cfg or {}).get("tz", USER_TZ))
        raise ToolError("Calendar isn't set up — ask the owner to "
                        "configure it.")


def _parse_gws_event(e, tz_name):
    """Parse one hatch_gws_cli +agenda event dict -> CalendarEvent."""
    tz = _tz(tz_name)
    title = (e.get("summary") or "(no title)").strip()
    location = (e.get("location") or "").strip()
    calendar = (e.get("calendar") or "").strip()
    if e.get("all_day"):
        start = datetime.strptime(e["start"], "%Y-%m-%d").replace(tzinfo=tz)
        return CalendarEvent(title=title, start=start, all_day=True,
                             location=location, calendar=calendar)
    starts = e.get("event_starts_at") or {}
    ends = e.get("event_ends_at") or {}
    s = starts.get("user_local") or e.get("start")
    en = ends.get("user_local") or e.get("end")
    start = datetime.fromisoformat(s)
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz)
    end = None
    if en:
        end = datetime.fromisoformat(en)
        if end.tzinfo is None:
            end = end.replace(tzinfo=tz)
    return CalendarEvent(title=title, start=start, end=end,
                         location=location, calendar=calendar)


class GwsCliCalendarProvider(CalendarProvider):
    """Read-only Google Calendar via the hatch_gws_cli connector CLI.

    `runner` is injectable for tests: fn(argv) -> parsed JSON dict.
    Raises CalendarNotConfigured when the CLI is missing or the account
    isn't connected — the message tells the user exactly what to do.
    """

    def __init__(self, account=None, tz_name=USER_TZ, runner=None):
        self.account = account
        self.tz_name = tz_name
        self._runner = runner or self._default_runner

    @staticmethod
    def _default_runner(argv):
        binary = shutil.which("hatch_gws_cli")
        if not binary:
            raise CalendarNotConfigured(CALENDAR_CONNECT_HELP)
        try:
            proc = subprocess.run(
                [binary] + argv, capture_output=True, text=True, timeout=25)
        except (OSError, subprocess.TimeoutExpired):
            raise CalendarNotConfigured(
                "I couldn't reach your calendar just now — try again "
                "in a bit.")
        if proc.returncode != 0:
            out = (proc.stdout + proc.stderr).lower()
            if any(k in out for k in ("not_connected", "not connected",
                                      "unauthorized", "auth", "connect")):
                raise CalendarNotConfigured(CALENDAR_CONNECT_HELP)
            raise CalendarNotConfigured(
                "I couldn't read your calendar just now — try again "
                "in a bit.")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise CalendarNotConfigured(
                "I couldn't read your calendar just now — try again "
                "in a bit.")

    def _status_ok(self):
        try:
            st = self._runner(["calendar", "status"])
        except CalendarNotConfigured:
            raise
        except Exception:
            raise CalendarNotConfigured(
                "I couldn't reach your calendar just now — try again "
                "in a bit.")
        if isinstance(st, dict) and st.get("status") == "connected":
            return True
        raise CalendarNotConfigured(CALENDAR_CONNECT_HELP)

    def agenda(self, when="today"):
        when = (when or "today").lower()
        if when not in ("today", "tomorrow", "week"):
            when = "today"
        self._status_ok()
        argv = ["calendar", "+agenda", f"--{when}", "--format", "json"]
        if self.account:
            argv += ["--account", self.account]
        try:
            data = self._runner(argv)
        except CalendarNotConfigured:
            raise
        except Exception:
            raise CalendarNotConfigured(
                "I couldn't read your calendar just now — try again "
                "in a bit.")
        events = []
        for e in (data.get("events") if isinstance(data, dict) else []) or []:
            try:
                events.append(_parse_gws_event(e, self.tz_name))
            except Exception as ex:  # one bad event must not kill the day
                _log(f"skipping unparseable calendar event: {ex}")
        events.sort(key=lambda ev: ev.start)
        return events


def format_events(events, when="today", tz_name=USER_TZ):
    """Telegram-friendly agenda text. Provider-neutral, no raw IDs."""
    when = (when or "today").lower()
    if not events:
        label = {"today": "today", "tomorrow": "tomorrow",
                 "week": "this week"}.get(when, "that window")
        return f"Nothing on your calendar {label}."
    tz = _tz(tz_name)
    head = {"today": "Today", "tomorrow": "Tomorrow",
            "week": "This week"}.get(when, "Upcoming")
    lines = [f"{head}:"]
    last_day = None
    for ev in events:
        local = ev.start.astimezone(tz)
        day = local.strftime("%a %d %b")
        if when == "week" and day != last_day:
            lines.append(f"— {day} —")
            last_day = day
        if ev.all_day:
            when_s = "all day"
        elif ev.end:
            end_l = ev.end.astimezone(tz)
            when_s = (f"{local.strftime('%-I:%M %p')}–"
                      f"{end_l.strftime('%-I:%M %p')}")
        else:
            when_s = local.strftime("%-I:%M %p")
        loc = f" @ {ev.location}" if ev.location else ""
        lines.append(f"• {when_s}: {ev.title}{loc}")
    return _truncate("\n".join(lines), MAX_TOOL_OUTPUT_CHARS)


def calendar_read(when="today", provider=None):
    """Read-only calendar lookup. `provider` injectable (tests)."""
    prov = provider or GwsCliCalendarProvider()
    return format_events(prov.agenda(when), when)


# ---------------------------------------------------------------------------
# Tool registry, schemas, catalog text, and the agentic loop
# ---------------------------------------------------------------------------

def _tool_web_search(args, ctx):
    return web_search(args.get("query", ""), int(args.get("count", 5) or 5))


def _tool_weather(args, ctx):
    return get_weather(args.get("city", ""), int(args.get("days", 2) or 2))


def _tool_news(args, ctx):
    return get_news(args.get("topic"), int(args.get("count", 6) or 6))


def _tool_calendar_read(args, ctx):
    provider = (ctx or {}).get("calendar_provider")
    return calendar_read(args.get("when", "today"), provider=provider)


def _tool_create_reminder(args, ctx):
    ctx = ctx or {}
    store = ctx.get("reminder_store") or ReminderStore()
    rem = store.create(ctx.get("user_id", 0), ctx.get("chat_id", 0),
                       args.get("text", ""), args.get("when", ""),
                       tz_name=(ctx.get("tz") or USER_TZ))
    tz = _tz(ctx.get("tz") or USER_TZ)
    dt = datetime.fromisoformat(rem["remind_at"]).astimezone(tz)
    return (f"Reminder set for {dt.strftime('%a %d %b, %I:%M %p')}: "
            f"{rem['text']} (#{rem['id']})")


def _tool_list_reminders(args, ctx):
    ctx = ctx or {}
    store = ctx.get("reminder_store") or ReminderStore()
    return _fmt_reminder_list(store.list(ctx.get("user_id", 0)),
                              tz_name=(ctx.get("tz") or USER_TZ))


def _tool_cancel_reminder(args, ctx):
    ctx = ctx or {}
    store = ctx.get("reminder_store") or ReminderStore()
    store.cancel(int(args.get("id", 0)), ctx.get("user_id", 0))
    return f"Reminder #{args.get('id')} cancelled."


TOOL_SCHEMAS = [
    {"name": "web_search",
     "description": "Search the web for current facts, releases, prices, docs.",
     "read_only": True,
     "parameters": {"type": "object",
                    "properties": {
                        "query": {"type": "string",
                                  "description": "search query"},
                        "count": {"type": "integer", "default": 5}},
                    "required": ["query"]},
     "function": _tool_web_search},
    {"name": "weather",
     "description": "Current weather + brief outlook for a city.",
     "read_only": True,
     "parameters": {"type": "object",
                    "properties": {
                        "city": {"type": "string"},
                        "days": {"type": "integer", "default": 2}},
                    "required": ["city"]},
     "function": _tool_weather},
    {"name": "news",
     "description": "Latest tech headlines; optional topic filter.",
     "read_only": True,
     "parameters": {"type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "count": {"type": "integer", "default": 6}},
                    "required": []},
     "function": _tool_news},
    {"name": "calendar_read",
     "description": "Read the user's Google Calendar agenda.",
     "read_only": True,
     "parameters": {"type": "object",
                    "properties": {
                        "when": {"type": "string",
                                 "enum": ["today", "tomorrow", "week"],
                                 "default": "today"}},
                    "required": []},
     "function": _tool_calendar_read},
    {"name": "create_reminder",
     "description": ("Set a reminder for the user. ONLY call when the user "
                     "explicitly asked for a reminder. Confirm the time "
                     "before creating when ambiguous."),
     "read_only": False,
     "parameters": {"type": "object",
                    "properties": {
                        "text": {"type": "string",
                                 "description": "what to remind about"},
                        "when": {"type": "string",
                                 "description": "e.g. 'in 20 minutes', "
                                                "'at 5pm', 'tomorrow at 9am', "
                                                "ISO-8601"}},
                    "required": ["text", "when"]},
     "function": _tool_create_reminder},
    {"name": "list_reminders",
     "description": "List the user's pending reminders.",
     "read_only": False,
     "parameters": {"type": "object", "properties": {},
                    "required": []},
     "function": _tool_list_reminders},
    {"name": "cancel_reminder",
     "description": "Cancel a pending reminder by its #id.",
     "read_only": False,
     "parameters": {"type": "object",
                    "properties": {"id": {"type": "integer"}},
                    "required": ["id"]},
     "function": _tool_cancel_reminder},
]

_TOOLS_BY_NAME = {t["name"]: t for t in TOOL_SCHEMAS}


def _catalog_lines():
    lines = []
    for t in TOOL_SCHEMAS:
        ro = "read-only" if t["read_only"] else "ACTING — explicit request only"
        lines.append(f"- {t['name']} ({ro}): {t['description']}")
    return "\n".join(lines)


TOOL_CATALOG_TEXT = (
    "You have tools for live information. Use them when the user asks about "
    "current facts, weather, news, their calendar, or wants a reminder — "
    "never for chit-chat, opinions, or things you already know.\n"
    "Available tools:\n" + _catalog_lines() + "\n"
    "To call tools, reply with one fenced block per call, in this exact form:\n"
    "```tool\n"
    '{"name": "web_search", "arguments": {"query": "..."}}\n'
    "```\n"
    "Rules: max 3 calls per reply. Read-only tools run automatically. "
    "create_reminder / list_reminders / cancel_reminder run ONLY when the "
    "user explicitly asked for a reminder — never unprompted. If no tool "
    "is needed, reply with exactly: NONE")


def build_probe_messages(messages):
    """Append the tool-probe instruction to a copy of `messages`."""
    probe = list(messages) + [
        {"role": "system", "content": TOOL_CATALOG_TEXT + (
            "\nDecide which tools (if any) the latest user message needs. "
            "Reply with ONLY tool-call blocks or the word NONE — no other "
            "text.")}]
    return probe


_TOOL_BLOCK_RE = re.compile(r"```tool\s*\n(.*?)```", re.DOTALL)


def parse_tool_calls(text, max_calls=MAX_CALLS_PER_PASS):
    """Extract validated (name, arguments) tool calls from model text.

    Unknown tools and malformed args are skipped with a note; they never
    execute. Returns (calls, notes)."""
    calls, notes = [], []
    for m in _TOOL_BLOCK_RE.finditer(text or ""):
        if len(calls) >= max_calls:
            notes.append("extra tool calls dropped (limit 3)")
            break
        try:
            obj = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            notes.append("malformed tool call ignored")
            continue
        name = obj.get("name")
        args = obj.get("arguments") or {}
        if name not in _TOOLS_BY_NAME:
            notes.append(f"unknown tool '{name}' ignored")
            continue
        if not isinstance(args, dict):
            notes.append(f"bad arguments for '{name}' ignored")
            continue
        calls.append((name, args))
    return calls, notes


def execute_tool_call(name, arguments, ctx=None, allow_acting=False):
    """Run one validated tool call. Returns a result string (never raises).

    Acting tools are refused unless allow_acting=True. All failures become
    provider-neutral text the model can relay or work around."""
    spec = _TOOLS_BY_NAME.get(name)
    if spec is None:
        return f"Tool '{name}' is not available."
    if not spec["read_only"] and not allow_acting:
        return (f"Tool '{name}' needs the user's explicit request — "
                "it was not run.")
    try:
        return _truncate(str(spec["function"](arguments or {}, ctx or {})))
    except ToolError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001 — tools must never break a turn
        _log(f"tool {name} crashed: {type(e).__name__}: {e}")
        return "That tool hit a snag — try again in a bit."


def _results_block(gathered, final=True):
    parts = [("[Tool results \u2014 answer the user naturally using these; "
              "never mention tools, providers, or how you got this:]"
              if final else
              "[Tool results so far \u2014 you may call more tools or reply NONE:]")]
    for name, args, result in gathered:
        arg_s = ", ".join(f"{k}={v}" for k, v in (args or {}).items())
        parts.append(f"\u2022 {name}({arg_s}):\n{result}")
    return _truncate("\n".join(parts), MAX_RESULTS_CHARS)


def maybe_run_tools(messages, complete_fn, user_text="", allow_acting=False,
                    on_tool=None, ctx=None, max_passes=MAX_PASSES):
    """Agentic tool loop — see module docstring for the full contract.

    Returns a results block to append to `messages` before the normal
    reply call, or None when no tools were needed/possible. Never raises.

    Pass 1: probe the model for tool calls, execute them. Pass 2
    (optional): feed pass-1 results back so the model can chain tools
    (e.g. web_search -> weather). A NONE (or no tool blocks) ends the
    loop early. Hard caps: max_passes probe passes, MAX_CALLS_PER_PASS
    executions per pass, PASS_TIMEOUT seconds per pass.
    """
    # Cheap-talk guard lives with the integrator; double-guard here anyway:
    # tool calls for one-word acks are pure waste.
    if len((user_text or "").split()) < 2:
        return None
    # Acting gate: the model may still ask, but without allow_acting the
    # acting calls are refused inside execute_tool_call. Additionally, if
    # the user text doesn't look like an explicit reminder request, force
    # the probe to read-only by stripping acting tools from the catalog.
    read_only_probe = (
        not allow_acting and not REMINDER_REQUEST_RE.search(user_text or ""))
    max_passes = max(1, int(max_passes or 1))

    gathered = []  # (name, args, result)
    try:
        for pass_no in range(max_passes):
            deadline = time.monotonic() + PASS_TIMEOUT
            if read_only_probe:
                probe_msgs = _read_only_probe(messages)
            else:
                probe_msgs = build_probe_messages(messages)
            if pass_no > 0:
                # Feed pass-1 results back so the model can chain tools
                # (e.g. web_search -> weather).
                probe_msgs = probe_msgs + [
                    {"role": "user",
                     "content": _results_block(gathered, final=False)}]
            try:
                probe_text = (complete_fn(probe_msgs) or "").strip()
            except Exception as e:
                _log(f"probe call failed: {type(e).__name__}: {e}")
                break
            if probe_text.upper() == "NONE" or not probe_text:
                break
            calls, notes = parse_tool_calls(probe_text)
            if notes:
                _log("probe notes: " + "; ".join(notes))
            if not calls:
                break
            for name, args in calls:
                if time.monotonic() > deadline:
                    _log("tool pass timed out — partial results kept")
                    break
                result = execute_tool_call(name, args, ctx=ctx,
                                           allow_acting=allow_acting)
                gathered.append((name, args, result))
                if on_tool:
                    try:
                        on_tool(name, args)
                    except Exception:
                        pass
                _log(f"tool {name} -> {len(result)} chars")
    except Exception as e:  # noqa: BLE001 — the turn must survive
        _log(f"tool loop failed: {type(e).__name__}: {e}")

    if not gathered:
        return None
    return _results_block(gathered, final=True)


def _read_only_probe(messages):
    """Probe variant with acting tools removed from the catalog (first
    gate for non-explicit reminder requests)."""
    lines = []
    for t in TOOL_SCHEMAS:
        if t["read_only"]:
            lines.append(f"- {t['name']}: {t['description']}")
    catalog = (
        "You have read-only tools for live information. Use them when the "
        "user asks about current facts, weather, news, or their calendar — "
        "never for chit-chat.\nAvailable tools:\n" + "\n".join(lines) +
        "\nReply with one fenced block per call:\n```tool\n"
        '{"name": "web_search", "arguments": {"query": "..."}}\n```\n'
        "Max 3 calls. You cannot set reminders in this mode. "
        "If no tool is needed, reply with exactly: NONE")
    return list(messages) + [
        {"role": "system",
         "content": catalog + "\nReply with ONLY tool-call blocks or NONE."}]
