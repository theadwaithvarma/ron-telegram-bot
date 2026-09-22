#!/usr/bin/env python3
"""Pooled keep-alive HTTPS connections through the egress proxy.

Why this exists: every urllib request opens a fresh connection. Through
this VM's proxy that's ~0.7s of CONNECT + TLS handshake per Telegram API
call, and a single bot reply makes 3-6 of them (typing, empty draft,
draft updates, final send) — all before the AI even starts generating.
A small pool of persistent keep-alive connections cuts each call to
~0.05s, shaving ~2s off every reply with zero behavior change.

Thread-safety: connections are checked out and back in; exactly one
thread owns a connection at a time. A connection the server closed while
idle is discarded and the request retried once on a fresh connection —
but only when no response was received, so a send is never duplicated.

Streaming: stream() hands back a _PooledStream wrapping the live
response for incremental (SSE) reading. close() drains the tail
(bounded) and recycles the connection; abort() discards it. Every
stream() must end in exactly one close()/abort().

Proxy handling mirrors urllib: honors no_proxy via proxy_bypass() and
reads the proxy (with credentials) from the https_proxy/https_proxy env
vars, exactly like the rest of the daemon.
"""
import base64
import http.client
import os
import queue
import threading
import urllib.parse
import urllib.request

_USER_AGENT = "ron-bot/1"

# Cap on draining a finished stream before the connection is recycled.
# Providers terminate the chunked SSE response right after `data: [DONE]`,
# so the drain is normally instant; this only bounds the pathological
# case of a server that never terminates (then the conn is discarded).
_DRAIN_TIMEOUT_SECS = 3.0


def _proxy_for(host):
    """Return (proxy_host, proxy_port, auth_header) or None for direct."""
    try:
        if urllib.request.proxy_bypass(host):
            return None
    except Exception:
        pass
    raw = (urllib.request.getproxies().get("https")
           or urllib.request.getproxies().get("http")
           or os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
           or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY"))
    if not raw:
        return None
    u = urllib.parse.urlsplit(raw if "://" in raw else "http://" + raw)
    auth = None
    if u.username:
        cred = (urllib.parse.unquote(u.username) + ":"
                + urllib.parse.unquote(u.password or ""))
        auth = "Basic " + base64.b64encode(cred.encode()).decode()
    return (u.hostname, u.port or 3128, auth)


def _close_quietly(conn):
    try:
        conn.close()
    except Exception:
        pass


class _RetryableConnError(Exception):
    """The pooled connection died while WRITING the request (stale idle
    connection closed by the server). The server never saw the bytes, so
    retrying on a fresh connection cannot duplicate the send."""


class _ConnPool:
    """Keep-alive connection pool for one HTTPS host."""

    def __init__(self, host):
        self.host = host
        self.proxy = _proxy_for(host)
        self._idle = queue.Queue()  # unbounded is fine: checkouts always pair with checkins

    def _new(self):
        if self.proxy:
            ph, pp, auth = self.proxy
            c = http.client.HTTPSConnection(ph, pp, timeout=65)
            hdrs = {"Proxy-Authorization": auth} if auth else None
            # CONNECT tunnel: the proxy only sees the hostname; the real
            # request (path included) travels inside TLS.
            c.set_tunnel(self.host, 443, headers=hdrs)
        else:
            c = http.client.HTTPSConnection(self.host, 443, timeout=65)
        return c

    def _checkout(self):
        try:
            return self._idle.get_nowait()
        except queue.Empty:
            return self._new()

    def _checkin(self, conn):
        try:
            self._idle.put_nowait(conn)
        except queue.Full:  # pragma: no cover - unbounded queue
            _close_quietly(conn)

    def _request_once(self, method, path, body, headers, timeout):
        """One attempt: returns (conn, resp).

        Raises _RetryableConnError when the pooled connection died during
        the write (stale idle connection — safe to retry fresh). Raises
        the original exception when getresponse() failed (the request was
        fully written; the server may have processed it — the caller must
        NOT retry, or a POST like sendMessage could land twice).
        """
        conn = self._checkout()
        try:
            conn.timeout = timeout
            conn.request(method, path, body=body, headers=headers)
        except Exception as e:
            _close_quietly(conn)
            raise _RetryableConnError(e) from e
        try:
            return conn, conn.getresponse()
        except Exception:
            _close_quietly(conn)
            raise

    def request(self, method, path, body, headers, timeout=65):
        """Send one request and return (status, body_bytes).

        headers is the full header dict (must include Content-Type when a
        body is sent). Retries once on a stale pooled connection — only
        when the write itself failed, so a POST is never sent twice.
        """
        hdrs = dict(headers or {})
        hdrs.setdefault("User-Agent", _USER_AGENT)
        hdrs["Connection"] = "keep-alive"
        last_err = None
        for attempt in (0, 1):
            try:
                if attempt == 0:
                    conn, resp = self._request_once(
                        method, path, body, hdrs, timeout)
                else:
                    conn = self._new()
                    try:
                        conn.timeout = timeout
                        conn.request(method, path, body=body, headers=hdrs)
                        resp = conn.getresponse()
                    except _RetryableConnError as e:
                        _close_quietly(conn)
                        last_err = e
                        continue
                    except Exception:
                        _close_quietly(conn)
                        raise
            except _RetryableConnError as e:
                # Stale pooled connection died on write — the server never
                # saw the bytes. Discard and retry once on a fresh conn.
                last_err = e
                continue
            except Exception:
                # getresponse() failed: the request was fully written and
                # the server may have processed it — do NOT retry.
                raise
            try:
                data = resp.read()  # must fully drain before reuse
            except Exception as e:
                # A response started arriving — do NOT retry (the send may
                # have landed); surface the failure to the caller.
                _close_quietly(conn)
                raise
            self._checkin(conn)
            return resp.status, data
        raise last_err  # both attempts failed before any response

    def post(self, path, body, content_type, timeout=65, extra_headers=None):
        """POST and return (status, body_bytes). Retries once on a stale
        pooled connection — only when no response arrived, so nothing is
        ever sent twice."""
        headers = {"Content-Type": content_type}
        headers.update(extra_headers or {})
        return self.request("POST", path, body, headers, timeout)

    def get(self, path, headers=None, timeout=65):
        """GET and return (status, body_bytes). Same stale-retry semantics."""
        return self.request("GET", path, None, headers, timeout)

    def stream(self, method, path, body, headers, timeout=65):
        """Open a streaming request; returns a _PooledStream.

        Raises on connection failure (after one stale-retry, like
        request()). The caller MUST end every stream with exactly one
        close() (drains + recycles the connection) or abort() (discards
        it) — the _PooledStream context manager does this automatically.
        """
        hdrs = dict(headers or {})
        hdrs.setdefault("User-Agent", _USER_AGENT)
        hdrs["Connection"] = "keep-alive"
        last_err = None
        for attempt in (0, 1):
            try:
                if attempt == 0:
                    conn, resp = self._request_once(
                        method, path, body, hdrs, timeout)
                else:
                    conn = self._new()
                    try:
                        conn.timeout = timeout
                        conn.request(method, path, body=body, headers=hdrs)
                        resp = conn.getresponse()
                    except Exception:
                        _close_quietly(conn)
                        raise
            except _RetryableConnError as e:
                # Stale pooled connection died on write — safe to retry
                # fresh; the server never saw the bytes.
                last_err = e
                continue
            except Exception:
                # getresponse() failed after a full write — the send may
                # have landed. Never retry; surface it.
                raise
            return _PooledStream(self, conn, resp)
        raise last_err


class _PooledStream:
    """A live HTTP response over a pooled keep-alive connection.

    Exactly one owner at a time. close() drains the rest of the response
    (bounded by _DRAIN_TIMEOUT_SECS) and returns the connection to the
    pool; abort() closes and discards it. Use as a context manager for
    automatic handling: clean exit -> close(), exception -> abort().
    """

    def __init__(self, pool, conn, resp):
        self._pool = pool
        self._conn = conn
        self._resp = resp
        self._done = False

    # Read passthrough for SSE consumers (readline loop etc.).
    def readline(self, *args):
        return self._resp.readline(*args)

    def read(self, *args):
        return self._resp.read(*args)

    def __iter__(self):
        return self._resp.__iter__()

    @property
    def status(self):
        return self._resp.status

    def getheader(self, *args):
        return self._resp.getheader(*args)

    def close(self):
        """End the stream cleanly: drain, then recycle the connection."""
        if self._done:
            return
        self._done = True
        try:
            # Bound the drain: providers terminate the chunk right after
            # `data: [DONE]`, so this is normally instant. (The next
            # checkout resets conn.timeout anyway.)
            self._conn.timeout = _DRAIN_TIMEOUT_SECS
            self._resp.read()
        except Exception:
            _close_quietly(self._conn)
            return
        self._pool._checkin(self._conn)

    def abort(self):
        """End the stream uncleanly: discard the connection, never reuse."""
        if self._done:
            return
        self._done = True
        _close_quietly(self._conn)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.close()
        else:
            self.abort()
        return False


_pools = {}
_pools_lock = threading.Lock()


def pool_for(host):
    """Return the shared pool for a host (created on first use)."""
    with _pools_lock:
        pool = _pools.get(host)
        if pool is None:
            pool = _ConnPool(host)
            _pools[host] = pool
        return pool
