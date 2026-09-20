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

    def post(self, path, body, content_type, timeout=65):
        """POST and return (status, body_bytes). Retries once on a stale
        pooled connection — only when no response arrived, so nothing is
        ever sent twice."""
        headers = {"Content-Type": content_type,
                   "User-Agent": _USER_AGENT,
                   "Connection": "keep-alive"}
        last_err = None
        for attempt in (0, 1):
            conn = self._checkout() if attempt == 0 else self._new()
            try:
                conn.timeout = timeout
                conn.request("POST", path, body=body, headers=headers)
                resp = conn.getresponse()
            except Exception as e:
                # Nothing came back — the pooled connection was probably
                # closed by the server while idle. Discard and retry fresh.
                last_err = e
                _close_quietly(conn)
                continue
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
