"""Web search backend for /factcheck — free-first, keyed upgrade when available.

Preference order:
  1. Tavily (https://tavily.com — free 1,000 searches/month, no card) when
     its key is connected in the vault slot ``custom.tavily``.
  2. Keyless DuckDuckGo lite scrape — no signup, no key, works today.

Returns a list of {"title", "url", "snippet"}. Raises RuntimeError when no
backend produces results. Only stdlib — no new dependencies.
"""

import html as _html
import json as _json
import re as _re
import urllib.parse as _up
import urllib.request as _ur

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_TAVILY_URL = "https://api.tavily.com/search"
_DDG_URL = "https://lite.duckduckgo.com/lite/?q="
_MAX_RESULTS = 6


def _vault_key(slot):
    """Raw surrogate for a vault slot, or None if not connected."""
    try:
        from dynamic_credentials import (dynamic_credential_entry,
                                         DynamicCredentialError)
        return str(dynamic_credential_entry(slot)["surrogate"]).strip()
    except Exception:
        return None


def _clean(text):
    return _re.sub(r"\s+", " ", _html.unescape(
        _re.sub(r"<[^>]+>", "", text or ""))).strip()


def _unwrap_ddg(href):
    """DuckDuckGo wraps result links: //duckduckgo.com/l/?uddg=<real url>."""
    m = _re.search(r"uddg=([^&]+)", href or "")
    if m:
        return _up.unquote(m.group(1))
    href = (href or "").strip()
    if href.startswith("//"):
        return "https:" + href
    return href


def _tavily_search(query):
    key = _vault_key("custom.tavily")
    if not key:
        return None
    body = _json.dumps({
        # api_key goes in the JSON body per Tavily's API; the value here is
        # the vault surrogate — the egress proxy swaps in the real key.
        "api_key": key,
        "query": query,
        "max_results": _MAX_RESULTS,
        "include_answer": False,
        "search_depth": "basic",
    }).encode()
    req = _ur.Request(_TAVILY_URL, data=body,
                      headers={"Content-Type": "application/json",
                               "User-Agent": _UA})
    with _ur.urlopen(req, timeout=30) as resp:
        data = _json.loads(resp.read().decode())
    out = []
    for r in data.get("results", [])[:_MAX_RESULTS]:
        out.append({"title": _clean(r.get("title")),
                    "url": r.get("url") or "",
                    "snippet": _clean(r.get("content"))[:400]})
    return out or None


def _ddg_search(query):
    url = _DDG_URL + _up.quote_plus(query)
    last_err = None
    for attempt in range(2):
        if attempt:
            # DDG momentarily throttles bursts — one short backoff retry.
            import time as _time
            _time.sleep(4)
        req = _ur.Request(url, headers={"User-Agent": _UA,
                                        "Accept": "text/html"})
        try:
            with _ur.urlopen(req, timeout=30) as resp:
                page = resp.read().decode(errors="replace")
        except Exception as e:
            last_err = e
            continue
        out = _parse_ddg_lite(page)
        if out:
            return out
    if last_err is not None:
        raise RuntimeError(str(last_err)[:120])
    return None


def _parse_ddg_lite(page):
    # Lite markup: each result row holds the link, then a result-snippet td.
    rows = _re.findall(
        r'<a rel="nofollow" href="([^"]+)"[^>]*>(.*?)</a>'
        r'(?:.*?<td class=\'result-snippet\'>(.*?)</td>)?',
        page, _re.S)
    out = []
    for href, title, snippet in rows[:_MAX_RESULTS]:
        url = _unwrap_ddg(_html.unescape(href))
        if not url.startswith("http"):
            continue
        out.append({"title": _clean(title)[:160],
                    "url": url,
                    "snippet": _clean(snippet)[:400]})
    return out or None


def web_search(query):
    """Search the web for a fact-check claim. Tavily if keyed, else DDG.

    Returns [{"title", "url", "snippet"}]. Raises RuntimeError on failure.
    """
    query = (query or "").strip()
    if not query:
        raise RuntimeError("empty query")
    errors = []
    for name, fn in (("tavily", _tavily_search), ("duckduckgo", _ddg_search)):
        try:
            results = fn(query)
        except Exception as e:  # backend hiccup — try the next one
            errors.append(f"{name}: {str(e)[:80]}")
            continue
        if results:
            return results
        errors.append(f"{name}: no results")
    raise RuntimeError("web search failed (" + "; ".join(errors) + ")")


def format_evidence(results):
    """Numbered evidence block fed to the verdict model."""
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r['title']}\n    URL: {r['url']}\n"
                     f"    Snippet: {r['snippet'] or '(no snippet)'}")
    return "\n".join(lines)


def format_sources(results):
    """Telegram-Markdown source list appended after the verdict."""
    lines = []
    for i, r in enumerate(results, 1):
        title = _re.sub(r"[\[\]()]", "", r["title"])[:80] or "source"
        domain = _up.urlparse(r["url"]).netloc or "web"
        lines.append(f"{i}. [{title}]({r['url']}) — {domain}")
    return "\n".join(lines)
