#!/usr/bin/env python3
"""Perf/stability tests for the overnight maintenance pass (v1.16.0).

- AI transport reuses pooled keep-alive connections (no fresh
  proxy CONNECT + TLS per AI call); error classification (429/401/5xx)
  and semaphore release behavior are preserved.
- History rollover runs off the reply's critical path.
- note_group() debounces known_groups.json rewrites.

No network, no Telegram.
"""
import json
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import http_pool
import ron_ai
import ron_daemon


class FakeStreamConn:
    """Fake HTTPSConnection handing out canned SSE responses."""

    instances = []

    def __init__(self, host, *a, **k):
        self.host = host
        self.timeout = None
        self.closed = False
        self.request_args = None
        FakeStreamConn.instances.append(self)

    def connect(self):
        pass

    def set_tunnel(self, host, port=None, headers=None):
        pass  # the CONNECT tunnel is a no-op on the fake

    def request(self, method, path, body=None, headers=None):
        self.request_args = (method, path, body, headers)

    def getresponse(self):
        return FakeStreamResp(self)

    def isclosed(self):
        return self.closed

    def close(self):
        self.closed = True


class FakeStreamResp:
    def __init__(self, conn):
        self.conn = conn
        self.status = 200
        self._lines = [
            b'data: {"choices": [{"delta": {"content": "hello"}}]}\n\n',
            b'data: [DONE]\n\n',
        ]

    def readline(self):
        return self._lines.pop(0) if self._lines else b""

    def read(self, amt=None):
        data = b"".join(self._lines)
        self._lines = []
        return data

    def isclosed(self):
        return False


class PooledTransportTest(unittest.TestCase):
    """ron_ai._request over the keep-alive pool (mocked sockets)."""

    def setUp(self):
        FakeStreamConn.instances = []
        self._saved = dict(http_pool._pools)
        http_pool._pools.clear()
        self._patcher = mock.patch("http.client.HTTPSConnection",
                                   FakeStreamConn)
        self._patcher.start()
        self._cooldowns = dict(ron_ai._provider_cooldown)
        ron_ai._provider_cooldown.clear()
        self._sem_before = ron_ai._API_SEMAPHORE._value

    def tearDown(self):
        self._patcher.stop()
        http_pool._pools.clear()
        http_pool._pools.update(self._saved)
        ron_ai._provider_cooldown.clear()
        ron_ai._provider_cooldown.update(self._cooldowns)

    def _cfg(self):
        return {"providers": [{"name": "groq",
                               "models": ["gpt-oss-120b"]}]}

    def _request(self, stream=True):
        with mock.patch.object(ron_ai, "_provider_key",
                               return_value="hsurr:k"):
            return ron_ai._request(
                self._cfg(), [{"role": "user", "content": "hi"}],
                stream=stream, timeout=30)

    def test_stream_reuses_connection_and_releases_semaphore(self):
        resp = self._request(stream=True)
        text = ron_ai._drain_stream(resp, None, None)
        self.assertIn("hello", text)
        conn1 = resp._resp._conn
        self.assertFalse(conn1.closed)
        resp2 = self._request(stream=True)
        ron_ai._drain_stream(resp2, None, None)
        # Second AI call reused the SAME keep-alive connection: no fresh
        # proxy CONNECT + TLS handshake.
        self.assertIs(resp2._resp._conn, conn1)
        self.assertEqual(len(FakeStreamConn.instances), 1)
        self.assertEqual(ron_ai._API_SEMAPHORE._value, self._sem_before)

    def test_429_sets_cooldown_and_raises_rate_limited(self):
        class R429(FakeStreamResp):
            def __init__(self, conn):
                super().__init__(conn)
                self.status = 429
                self._lines = []

            def read(self, amt=None):
                return b'{"error": {"message": "try again in 90s"}}'

        with mock.patch.object(FakeStreamConn, "getresponse",
                               lambda self: R429(self)):
            with self.assertRaises(ron_ai.RateLimitedError):
                self._request(stream=True)
        # Provider cooled down (>60s out), semaphore released.
        self.assertGreater(ron_ai._provider_cooldown.get("groq", 0),
                           time.monotonic() + 60)
        self.assertEqual(ron_ai._API_SEMAPHORE._value, self._sem_before)

    def test_401_skips_provider_with_backend_error(self):
        class R401(FakeStreamResp):
            def __init__(self, conn):
                super().__init__(conn)
                self.status = 401
                self._lines = []

            def read(self, amt=None):
                return b'{"error": {"message": "bad key"}}'

        with mock.patch.object(FakeStreamConn, "getresponse",
                               lambda self: R401(self)):
            with self.assertRaisesRegex(RuntimeError, "AI backend error"):
                self._request(stream=True)
        self.assertEqual(ron_ai._API_SEMAPHORE._value, self._sem_before)

    def test_500_retries_once_then_backend_error(self):
        class R500(FakeStreamResp):
            def __init__(self, conn):
                super().__init__(conn)
                self.status = 500
                self._lines = []

            def read(self, amt=None):
                return b'{"error": {"message": "boom"}}'

        with mock.patch.object(FakeStreamConn, "getresponse",
                               lambda self: R500(self)), \
             mock.patch("time.sleep") as slept:
            with self.assertRaisesRegex(RuntimeError, "AI backend error"):
                self._request(stream=True)
        slept.assert_called_once_with(ron_ai.RETRY_BACKOFF_SECS)
        self.assertEqual(ron_ai._API_SEMAPHORE._value, self._sem_before)

    def test_nonstream_200(self):
        class RJson(FakeStreamResp):
            def __init__(self, conn):
                super().__init__(conn)
                self._lines = []

            def read(self, amt=None):
                return json.dumps(
                    {"choices": [{"message": {"content": "yo"}}]}).encode()

        with mock.patch.object(FakeStreamConn, "getresponse",
                               lambda self: RJson(self)):
            resp = self._request(stream=False)
            with resp:
                data = json.loads(resp.read().decode())
        self.assertEqual(data["choices"][0]["message"]["content"], "yo")
        self.assertEqual(ron_ai._API_SEMAPHORE._value, self._sem_before)


class AsyncRolloverTest(unittest.TestCase):
    """History fold must not block the reply's critical path."""

    def test_prompt_turns_returns_while_fold_blocked(self):
        blocked = threading.Event()
        released = threading.Event()
        calls = []

        def fake_rollover(chat_id, prior):
            calls.append(chat_id)
            blocked.set()          # worker started...
            released.wait(timeout=15)  # ...then stuck here
            return prior

        stored = [{"role": "u", "content": f"m{i}"} for i in range(50)]
        with mock.patch.object(ron_ai, "load_history",
                               return_value={"123": stored}), \
             mock.patch.object(ron_ai, "_load_summary",
                               return_value="oldsum"), \
             mock.patch.object(ron_ai, "_maybe_rollover", fake_rollover), \
             mock.patch.object(ron_ai, "_ROLLOVER_RUNNING", set()):
            t0 = time.monotonic()
            turns, summary = ron_ai._prompt_turns(123, "hello", "u")
            dt = time.monotonic() - t0
        # Returned immediately with the prior summary while the fold is
        # still blocked — the reply never waits on summarization.
        self.assertLess(dt, 5)
        self.assertTrue(blocked.wait(timeout=15))
        self.assertEqual(summary, "oldsum")
        self.assertEqual(turns[-1]["content"], "[u]: hello")
        released.set()
        deadline = time.monotonic() + 15
        while not calls and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(calls, [123])

    def test_concurrent_turns_spawn_one_worker(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def fake_rollover(chat_id, prior):
            calls.append(chat_id)
            started.set()
            release.wait(timeout=15)
            return prior

        stored = [{"role": "u", "content": f"m{i}"} for i in range(50)]
        running = set()
        with mock.patch.object(ron_ai, "load_history",
                               return_value={"123": stored}), \
             mock.patch.object(ron_ai, "_load_summary",
                               return_value="s"), \
             mock.patch.object(ron_ai, "_maybe_rollover", fake_rollover), \
             mock.patch.object(ron_ai, "_ROLLOVER_RUNNING", running):
            ron_ai._prompt_turns(123, "a", "u")
            self.assertTrue(started.wait(timeout=15))
            ron_ai._prompt_turns(123, "b", "u")  # worker in flight: skip
            release.set()
            deadline = time.monotonic() + 15
            while running and time.monotonic() < deadline:
                time.sleep(0.05)
        self.assertEqual(calls, [123])  # exactly one fold


class NoteGroupDebounceTest(unittest.TestCase):
    """known_groups.json is not rewritten on every group message."""

    def setUp(self):
        self.writes = []
        self._orig_write = ron_daemon._atomic_write_json
        ron_daemon._atomic_write_json = lambda p, o: self.writes.append(p)
        self._orig_load = ron_daemon.load_known_groups
        self.groups = {}
        ron_daemon.load_known_groups = lambda: dict(self.groups)
        self._orig_ts = dict(ron_daemon._GROUP_WRITE_TS)
        ron_daemon._GROUP_WRITE_TS.clear()

    def tearDown(self):
        ron_daemon._atomic_write_json = self._orig_write
        ron_daemon.load_known_groups = self._orig_load
        ron_daemon._GROUP_WRITE_TS.clear()
        ron_daemon._GROUP_WRITE_TS.update(self._orig_ts)

    def _note(self, cid, title):
        # note_group writes via the mocked loader's dict; mirror the
        # in-memory state the way the real loader would after a write.
        ron_daemon.note_group(cid, title)
        if self.writes:
            self.groups[str(cid)] = {"title": title, "seen": time.time()}

    def test_repeat_same_title_writes_once(self):
        self._note(1, "Playground")
        self._note(1, "Playground")
        self._note(1, "Playground")
        self.assertEqual(len(self.writes), 1)

    def test_title_change_writes_again(self):
        self._note(1, "Playground")
        self._note(1, "Playground Renamed")
        self.assertEqual(len(self.writes), 2)

    def test_new_group_writes(self):
        self._note(1, "A")
        self._note(2, "B")
        self.assertEqual(len(self.writes), 2)


if __name__ == "__main__":
    unittest.main()
