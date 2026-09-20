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


if __name__ == "__main__":
    unittest.main()
