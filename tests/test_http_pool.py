#!/usr/bin/env python3
"""Tests for http_pool — keep-alive Telegram API connections.

No network, no Telegram.

Run: python3 -m unittest discover tests -v   (from ~/workspace/telegram-bot)
"""
import base64
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import http_pool


class FakeResp:
    def __init__(self, status=200, body=b'{"ok": true}'):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def getheaders(self):
        return []


class FakeConn:
    """Stands in for http.client.HTTPSConnection."""
    instances = []

    def __init__(self, host, port=None, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.tunnel = None
        self.requests = []
        self.fail_next_request = False
        self.closed = False
        FakeConn.instances.append(self)

    def set_tunnel(self, host, port=None, headers=None):
        self.tunnel = (host, port, headers)

    def request(self, method, path, body=None, headers=None):
        if self.fail_next_request:
            self.fail_next_request = False
            raise ConnectionResetError("stale pooled connection")
        self.requests.append((method, path, body, headers))

    def getresponse(self):
        return FakeResp()

    def close(self):
        self.closed = True


_PROXY_VARS = ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
               "no_proxy", "NO_PROXY")


class PoolTest(unittest.TestCase):
    def setUp(self):
        FakeConn.instances.clear()
        for k in _PROXY_VARS:
            os.environ.pop(k, None)
        http_pool._pools.clear()
        m = mock.patch("http.client.HTTPSConnection", FakeConn)
        m.start()
        self.addCleanup(m.stop)

    def _pool(self):
        return http_pool.pool_for("api.telegram.org")

    def test_reuses_connection_across_posts(self):
        pool = self._pool()
        s1, d1 = pool.post("/botX/getMe", b"",
                           "application/x-www-form-urlencoded")
        s2, d2 = pool.post("/botX/sendMessage", b"a=1",
                           "application/x-www-form-urlencoded")
        self.assertEqual((s1, d1), (200, b'{"ok": true}'))
        self.assertEqual((s2, d2), (200, b'{"ok": true}'))
        # second call reused the warm connection — no new handshake
        self.assertEqual(len(FakeConn.instances), 1)
        self.assertEqual(len(FakeConn.instances[0].requests), 2)

    def test_stale_connection_discarded_and_retried_once(self):
        pool = self._pool()
        pool.post("/botX/getMe", b"", "application/x-www-form-urlencoded")
        stale = FakeConn.instances[0]
        stale.fail_next_request = True  # server closed it while idle
        status, _data = pool.post("/botX/sendMessage", b"a=1",
                                  "application/x-www-form-urlencoded")
        self.assertEqual(status, 200)
        self.assertTrue(stale.closed)  # stale one discarded, not repooled
        self.assertEqual(len(FakeConn.instances), 2)  # one fresh replacement
        # pool now holds the fresh connection; next call reuses it
        pool.post("/botX/getMe", b"", "application/x-www-form-urlencoded")
        self.assertEqual(len(FakeConn.instances), 2)

    def test_no_retry_after_response_started(self):
        """Bytes already arrived — the send may have landed, never resend."""
        pool = self._pool()
        pool.post("/botX/getMe", b"", "application/x-www-form-urlencoded")
        conn = FakeConn.instances[0]

        class DyingResp(FakeResp):
            def read(self):
                raise ConnectionResetError("died mid-body")

        with mock.patch.object(FakeConn, "getresponse",
                               lambda slf: DyingResp()):
            with self.assertRaises(ConnectionResetError):
                pool.post("/botX/sendMessage", b"a=1",
                          "application/x-www-form-urlencoded")
        self.assertTrue(conn.closed)  # closed, NOT returned to the pool
        self.assertEqual(len(FakeConn.instances), 1)  # no retry -> no new conn
        # pool is empty now; the next post opens a fresh connection
        pool.post("/botX/getMe", b"", "application/x-www-form-urlencoded")
        self.assertEqual(len(FakeConn.instances), 2)

    def test_no_retry_when_getresponse_fails(self):
        """getresponse() failed after a full write — the server may have
        processed the POST. Must raise, not retry (no duplicate send)."""
        pool = self._pool()
        pool.post("/botX/getMe", b"", "application/x-www-form-urlencoded")
        conn = FakeConn.instances[0]
        with mock.patch.object(
                FakeConn, "getresponse",
                side_effect=ConnectionResetError("reset after write")):
            with self.assertRaises(ConnectionResetError):
                pool.post("/botX/sendMessage", b"a=1",
                          "application/x-www-form-urlencoded")
        # The write fully succeeded exactly once — no duplicate POST.
        self.assertEqual(len(conn.requests), 2)  # getMe + one sendMessage
        self.assertEqual(len(FakeConn.instances), 1)  # no fresh conn made

    def test_non_200_returned_not_raised(self):
        pool = self._pool()

        class R429(FakeResp):
            def __init__(self):
                super().__init__(429, b'{"ok": false, "parameters": '
                                      b'{"retry_after": 3}}')

        with mock.patch.object(FakeConn, "getresponse",
                               lambda self: R429()):
            status, data = pool.post("/botX/x", b"", "application/json")
        self.assertEqual(status, 429)
        self.assertIn(b"retry_after", data)

    def test_proxy_tunnel_with_auth(self):
        os.environ["https_proxy"] = "http://user:p%40ss@198.19.0.1:3128"
        pool = self._pool()
        pool.post("/botX/getMe", b"", "application/x-www-form-urlencoded")
        conn = FakeConn.instances[0]
        self.assertEqual((conn.host, conn.port), ("198.19.0.1", 3128))
        host, port, headers = conn.tunnel
        self.assertEqual((host, port), ("api.telegram.org", 443))
        expect = "Basic " + base64.b64encode(b"user:p@ss").decode()
        self.assertEqual(headers["Proxy-Authorization"], expect)

    def test_no_proxy_bypass_connects_direct(self):
        os.environ["https_proxy"] = "http://198.19.0.1:3128"
        os.environ["no_proxy"] = "api.telegram.org"
        pool = self._pool()
        pool.post("/botX/getMe", b"", "application/x-www-form-urlencoded")
        conn = FakeConn.instances[0]
        self.assertEqual((conn.host, conn.port), ("api.telegram.org", 443))
        self.assertIsNone(conn.tunnel)

    def test_thread_safe_checkout(self):
        """Concurrent posts never share one connection simultaneously."""
        pool = self._pool()
        owners = {}
        lock = threading.Lock()
        real_request = FakeConn.request

        def guarded_request(slf, *a, **k):
            with lock:
                self.assertNotIn(slf, owners,
                                 "connection used by two threads at once")
                owners[slf] = True
            try:
                return real_request(slf, *a, **k)
            finally:
                with lock:
                    del owners[slf]

        with mock.patch.object(FakeConn, "request", guarded_request):
            threads = [threading.Thread(
                target=pool.post,
                args=("/botX/getMe", b"",
                      "application/x-www-form-urlencoded"))
                for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        # never more connections than threads; all returned to the pool
        self.assertLessEqual(len(FakeConn.instances), 8)


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
            b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n',
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


class StreamTest(unittest.TestCase):
    """Pooled streaming: clean close recycles, abort discards."""

    def setUp(self):
        FakeStreamConn.instances = []
        self._saved = dict(http_pool._pools)
        http_pool._pools.clear()
        self._patcher = mock.patch("http.client.HTTPSConnection",
                                   FakeStreamConn)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        http_pool._pools.clear()
        http_pool._pools.update(self._saved)

    def _pool(self):
        return http_pool.pool_for("ai.example")

    def test_stream_close_recycles(self):
        pool = self._pool()
        s = pool.stream("POST", "/v1/chat", b"{}", {"X-A": "b"}, 30)
        self.assertEqual(s.status, 200)
        self.assertEqual(s._conn.request_args[0], "POST")
        self.assertEqual(s._conn.request_args[3]["X-A"], "b")
        lines = []
        while True:
            ln = s.readline()
            if not ln:
                break
            lines.append(ln)
        self.assertTrue(any(b"[DONE]" in ln for ln in lines))
        s.close()
        s.close()  # repeat close is a no-op
        s2 = pool.stream("POST", "/v1/chat", b"{}", None, 30)
        self.assertIs(s2._conn, s._conn)  # same connection reused
        self.assertFalse(s._conn.closed)
        s2.abort()

    def test_stream_abort_discards(self):
        pool = self._pool()
        s = pool.stream("POST", "/v1/chat", b"{}", None, 30)
        s.readline()  # partial read, then bail
        s.abort()
        s.abort()  # repeat abort is a no-op
        self.assertTrue(s._conn.closed)
        s2 = pool.stream("POST", "/v1/chat", b"{}", None, 30)
        self.assertIsNot(s2._conn, s._conn)  # fresh connection
        s2.abort()

    def test_stream_context_manager(self):
        pool = self._pool()
        with pool.stream("POST", "/v1/chat", b"{}", None, 30) as s:
            conn = s._conn
        self.assertFalse(conn.closed)  # clean exit -> recycled
        with self.assertRaises(RuntimeError):
            with pool.stream("POST", "/v1/chat", b"{}", None,
                             30) as s2:
                raise RuntimeError("boom")
        self.assertTrue(s2._conn.closed)  # exception -> discarded

    def test_stream_stale_retry(self):
        pool = self._pool()
        dead = FakeStreamConn("ai.example")
        pool._checkin(dead)

        def bad_request(*a, **k):
            raise ConnectionResetError("stale")
        dead.request = bad_request  # instance-level: fresh conns are fine
        s = pool.stream("POST", "/v1/chat", b"{}", None, 30)
        self.assertIsNot(s._conn, dead)
        self.assertEqual(s.status, 200)
        s.abort()

    def test_get(self):
        pool = self._pool()
        status, body = pool.get("/botX/getMe", {"X-Custom": "1"}, 30)
        self.assertEqual(status, 200)
        self.assertIsInstance(body, bytes)
        conn = FakeStreamConn.instances[-1]
        self.assertEqual(conn.request_args[0], "GET")
        self.assertEqual(conn.request_args[3]["X-Custom"], "1")

    def test_post_extra_headers(self):
        pool = self._pool()
        status, body = pool.post("/v1/chat", b"{}", "application/json",
                                 30, extra_headers={"X-Key": "v"})
        self.assertEqual(status, 200)
        conn = FakeStreamConn.instances[-1]
        self.assertEqual(conn.request_args[3]["X-Key"], "v")


if __name__ == "__main__":
    unittest.main()
