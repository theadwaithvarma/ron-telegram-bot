#!/usr/bin/env python3
"""Phase 6: per-chat semantic answer cache for @ronrebootedbot.

Sits in front of the AI chain (ron_daemon.ai_reply -> ron_ai.chat):
when the same (or nearly the same) question is asked twice in the same
chat, the second one is answered from the cache instead of burning
provider tokens.

Design notes:
- Strictly per-chat (Phase 4 memory isolation): cache keys are
  (chat_id, normalized_question), so nothing ever leaks across chats.
- stdlib only. One SQLite file + one JSON stats file, guarded by a
  single module-level lock. Message *contents* are never logged --
  only counts and chat ids.
"""

import json
import os
import re
import sqlite3
import threading
import time
import unicodedata

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

CACHE_TTL_SECS = 86400          # 24h: a cached answer older than this is stale
MAX_ENTRIES_PER_CHAT = 200      # LRU cap per chat
HIT_THRESHOLD = 0.80            # minimum similarity score to serve a cached reply
_LEV_CAP = 200                  # cap Levenshtein DP input length to bound cost

_HOME = os.path.expanduser("~")
_DIR = os.path.join(_HOME, "workspace", "telegram-bot")
_DB_NAME = "ron_cache.db"
_STATS_NAME = "ron_cache_stats.json"

# One lock guards every DB and stats access in this module, so concurrent
# lookups/stores from the daemon's threads can't interleave mid-write.
_LOCK = threading.Lock()

# ~45 common English words that carry no meaning for "are these the same
# question?" comparisons. Dropping them makes "what is the python" and
# "what is python" compare as identical.
STOPWORDS = frozenset(
    "a an the and or for of in on at to from with is are was be do have has "
    "what which who when where why how i you it they me my your our their "
    "this that these those not can will so more some all which s".split()
)

# Freshness-sensitive words: answers go stale fast, so never cache these.
# \b word boundaries stop "now" matching inside "know", etc.
_FRESH_RE = re.compile(
    r"\b(latest|today|tonight|now|current|currently|news|price|cost|weather|"
    r"score|tomorrow|yesterday)\b"
    r"|this week|right now"          # multi-word phrases
    r"|\d{1,2}/\d{1,2}"              # 21/09 style dates
    r"|\d{4}-\d{2}-\d{2}"            # 2026-09-21 style dates
    r"|\b(19|20)\d{2}\b"             # bare years like 2026
)

# Memory-write commands are actions, not questions -- caching their "answer"
# would be wrong.
_MEMWRITE_PREFIXES = ("remember", "forget that", "forget everything", "forget ")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    chat_id    TEXT NOT NULL,
    qnorm      TEXT NOT NULL,          -- normalize(question)
    reply      TEXT NOT NULL,
    created_ts REAL NOT NULL,         -- when first cached
    last_hit_ts REAL NOT NULL,        -- last served (drives LRU)
    hits       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (chat_id, qnorm)
);
CREATE INDEX IF NOT EXISTS idx_cache_chat_lru
    ON cache (chat_id, last_hit_ts);
"""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _default_db_path():
    os.makedirs(_DIR, exist_ok=True)
    return os.path.join(_DIR, _DB_NAME)


def _default_stats_path():
    os.makedirs(_DIR, exist_ok=True)
    return os.path.join(_DIR, _STATS_NAME)


def _connect(db_path):
    """Open the cache DB with settings safe for multi-threaded use."""
    path = db_path or _default_db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # check_same_thread=False because the module lock (not sqlite's own
    # thread check) is what serializes access.
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")   # readers don't block writers
    conn.execute("PRAGMA busy_timeout=5000")   # wait instead of failing fast
    conn.executescript(_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def normalize(text):
    """Canonical form of a question for cache keys and comparisons.

    NFKC folds unicode variants (full-width chars, ligatures) to a standard
    form; lowercasing + punctuation-stripping makes "What is Python?!" and
    "what is python" the same key. re.UNICODE \\w keeps letters from every
    script (Devanagari, Cyrillic, ...), not just ASCII.
    """
    t = unicodedata.normalize("NFKC", text or "")
    t = t.lower()
    t = re.sub(r"[^\w]", " ", t)          # punctuation -> space, keep letters
    t = re.sub(r"\s+", " ", t).strip()    # collapse whitespace
    return t


def _content_tokens(text):
    """Word tokens minus STOPWORDS -- the words that carry the meaning."""
    return {w for w in normalize(text).split() if w and w not in STOPWORDS}


def _levenshtein(a, b):
    """Edit distance, inputs truncated to _LEV_CAP chars to bound the DP."""
    a, b = a[:_LEV_CAP], b[:_LEV_CAP]
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    # Only two rows are kept in memory instead of the full matrix.
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1,        # deletion
                           cur[j - 1] + 1,    # insertion
                           prev[j - 1] + (ca != cb)))  # substitution
        prev = cur
    return prev[-1]


def _edit_sim(a, b):
    """Normalized edit similarity in [0, 1]; 1.0 means identical."""
    m = max(len(a), len(b))
    if m == 0:
        return 1.0
    return 1.0 - _levenshtein(a, b) / m


def similarity(a, b):
    """How alike two questions are, as a float in [0, 1].

    Token-set overlap (Szymkiewicz-Simpson coefficient: shared content words
    divided by the smaller set) catches paraphrases where one question is a
    subset of the other -- e.g. "what is python" vs "what is python used
    for" scores 1.0 on overlap, while pure character edit distance only
    reaches ~0.59 because of the extra words. That is why the short-query
    path takes the max of edit and overlap instead of pure edit: a literal
    pure-edit short path can never reach HIT_THRESHOLD on subset
    paraphrases.

    Two questions whose content words are identical (differing only by
    stopwords, e.g. "what is the python" vs "what is python") score 1.0.
    """
    na, nb = normalize(a), normalize(b)
    if na == nb:
        return 1.0
    ta, tb = _content_tokens(a), _content_tokens(b)
    if ta and ta == tb:
        return 1.0
    # Token-set overlap: |A cap B| / min(|A|, |B|). 0 when a side is empty.
    overlap = (len(ta & tb) / min(len(ta), len(tb))) if ta and tb else 0.0
    edit = _edit_sim(na, nb)
    if len(ta) < 3 or len(tb) < 3:
        # Short-query path: few content words, so word statistics are thin;
        # take the stronger of the two signals.
        return max(edit, overlap)
    return 0.8 * overlap + 0.2 * edit


def is_cacheable(text):
    """False for anything that must never be served from cache."""
    if not text or not text.strip():
        return False
    raw = text.strip()
    low = raw.lower()
    if low.startswith("/"):
        return False                       # bot commands
    if low.startswith(_MEMWRITE_PREFIXES):
        return False                       # memory-write actions
    if _FRESH_RE.search(low):
        return False                       # freshness-sensitive
    if len(normalize(text)) < 10:
        return False                       # too short to be a real question
    return True


def est_tokens(question, reply):
    """Rough token estimate: ~4 chars per token (OpenAI-style rule of thumb)."""
    return (len(question or "") + len(reply or "")) // 4


# ---------------------------------------------------------------------------
# Stats (JSON, atomic writes)
# ---------------------------------------------------------------------------

def _load_stats(stats_path):
    path = stats_path or _default_stats_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {"hits": int(data.get("hits", 0)),
                "misses": int(data.get("misses", 0)),
                "tokens_saved_est": int(data.get("tokens_saved_est", 0))}
    except (OSError, ValueError, AttributeError):
        # Missing or corrupt file: start from zero rather than crashing.
        return {"hits": 0, "misses": 0, "tokens_saved_est": 0}


def _save_stats(stats_path, stats):
    """Write stats atomically: tmp file + rename, so a crash can't corrupt it."""
    path = stats_path or _default_stats_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stats, f)
    os.replace(tmp, path)


def get_stats(stats_path=None):
    """Return {"hits", "misses", "tokens_saved_est"} (zeros if no file yet)."""
    with _LOCK:
        return _load_stats(stats_path)


def _bump_stats(stats_path, hits=0, misses=0, tokens=0):
    stats = _load_stats(stats_path)
    stats["hits"] += hits
    stats["misses"] += misses
    stats["tokens_saved_est"] += tokens
    _save_stats(stats_path, stats)


# ---------------------------------------------------------------------------
# Cache operations
# ---------------------------------------------------------------------------

def lookup(chat_id, text, db_path=None, stats_path=None):
    """Return the cached reply for a similar question in this chat, else None.

    Non-cacheable questions return None *without* touching stats or the DB.
    On a hit the row's last_hit_ts/hits are refreshed (feeds LRU) and stats
    count the hit plus estimated tokens saved.
    """
    if not is_cacheable(text):
        return None
    # Normalize the chat id to text: the daemon passes ints for DMs and
    # strings for some groups, and SQLite does NOT consider 123 = '123'.
    # Without this, the same chat could read and write different rows.
    chat_id = str(chat_id)
    qnorm = normalize(text)
    now = time.time()
    with _LOCK:
        conn = _connect(db_path)
        try:
            # Lazily drop this chat's expired rows on every lookup, so the
            # table can't fill with stale entries nobody reads.
            conn.execute("DELETE FROM cache WHERE chat_id = ? AND created_ts < ?",
                         (chat_id, now - CACHE_TTL_SECS))
            rows = conn.execute(
                "SELECT qnorm, reply, created_ts FROM cache WHERE chat_id = ?",
                (chat_id,)).fetchall()
            best, best_reply, best_qnorm = -1.0, None, None
            for stored_qnorm, reply, created_ts in rows:
                # Belt-and-braces: skip anything past TTL even if the lazy
                # delete above somehow missed it.
                if now - created_ts >= CACHE_TTL_SECS:
                    continue
                score = similarity(text, stored_qnorm)
                if score > best:
                    best, best_reply, best_qnorm = score, reply, stored_qnorm
            if best_reply is not None and best >= HIT_THRESHOLD:
                conn.execute(
                    "UPDATE cache SET last_hit_ts = ?, hits = hits + 1 "
                    "WHERE chat_id = ? AND qnorm = ?",
                    (now, chat_id, best_qnorm))
                conn.commit()
                _bump_stats(stats_path, hits=1,
                            tokens=est_tokens(text, best_reply))
                return best_reply
            _bump_stats(stats_path, misses=1)
            return None
        finally:
            conn.close()


def store(chat_id, text, reply, db_path=None):
    """Cache a question->reply pair for this chat. Returns True on success.

    Refuses (False, nothing written) non-cacheable questions and blank
    replies. Re-storing the same normalized question replaces the reply.
    Enforces MAX_ENTRIES_PER_CHAT with LRU eviction (oldest last_hit_ts
    first), scoped to this chat only.
    """
    if not is_cacheable(text):
        return False
    if not reply or not reply.strip():
        return False
    # Same chat_id normalization as lookup() — see the note there.
    chat_id = str(chat_id)
    qnorm = normalize(text)
    now = time.time()
    with _LOCK:
        conn = _connect(db_path)
        try:
            # INSERT OR REPLACE: same (chat_id, qnorm) updates the reply and
            # refreshes timestamps instead of duplicating the row.
            conn.execute(
                "INSERT OR REPLACE INTO cache "
                "(chat_id, qnorm, reply, created_ts, last_hit_ts, hits) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (chat_id, qnorm, reply, now, now))
            # LRU eviction for this chat only: keep the MAX_ENTRIES_PER_CHAT
            # most-recently-hit rows, delete the rest.
            evicted = conn.execute(
                "DELETE FROM cache WHERE chat_id = ? AND rowid NOT IN ("
                "  SELECT rowid FROM cache WHERE chat_id = ?"
                "  ORDER BY last_hit_ts DESC, rowid DESC LIMIT ?)",
                (chat_id, chat_id, MAX_ENTRIES_PER_CHAT)).rowcount
            conn.commit()
            if evicted:
                # Counts and ids only -- never message contents.
                print(f"[ron_cache] evicted {evicted} LRU entries "
                      f"for chat {chat_id}")
            return True
        finally:
            conn.close()
