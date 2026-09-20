#!/usr/bin/env python3
"""Phase 4 — per-chat isolated memory with encryption at rest.

LOCKED DESIGN (Adwaith, 2026-09-20):
- Every row is scoped by Telegram chat_id. Every read filters by chat_id.
  A DM's memory never leaks into a group or another DM. No exceptions.
- Encryption at rest: message text, fact text, and approval payloads are
  Fernet-encrypted. The key comes from RON_MEMORY_KEY (env). No key ->
  MemoryStore refuses to init (fail closed). The key and plaintext never
  appear in logs, errors, or the repo.
- Recall is FTS5 keyword search + decrypt on read. The FTS index is
  contentless (token vocabulary only, no document text stored) — a known
  v1 tradeoff, documented here. Bounded, zero LLM calls (token economy).
- Distillation v1: explicit "remember this" captures + deterministic
  contradiction replacement (keyword overlap -> old fact superseded and
  noted). No automatic LLM fact extraction (v2).
- Raw messages expire via prune_expired(); facts persist until forgotten
  or contradicted.

Cross-chat disclosure (refinement, 2026-09-20): matching a fact learned in
a DM with person P to P's Telegram user_id in a group happens ONLY to ask
P's consent PRIVATELY via DM. The fact stays stored under the DM's
chat_id — this is identity matching for consent, NOT memory merging.
Consent is recorded per (group_chat_id, user_id, fact_id): ask once.
"""
import base64
import hashlib
import os
import re
import sqlite3
import threading
import time

from cryptography.fernet import Fernet, InvalidToken

HOME = os.path.expanduser("~")
DB_PATH = os.path.join(HOME, "workspace", "telegram-bot", "ron_memory.db")
KEY_ENV = "RON_MEMORY_KEY"


class MemoryStoreError(Exception):
    """Raised when the store can't operate safely (no key, bad key, DB error)."""


_STOPWORDS = frozenset(
    "a an the and or but if then else when at by for with about into through "
    "during before after above below to from up down in out on off over under "
    "again further once here there all any both each few more most other some "
    "such no nor not only own same so than too very can will just don should "
    "now is are was were be been being have has had having do does did doing "
    "would could ought i you he she it we they them his her its our their "
    "this that these those am me my we us what which who whom whose "
    "also very much many get got like know think want need take make".split())


def content_words(text):
    """Significant lowercase words for overlap/contradiction checks."""
    return {w for w in re.findall(r"[a-zA-Z]{4,}", (text or "").lower())
            if w not in _STOPWORDS}


# Words that signal a new fact updates/replaces an older one about the same
# subject ("exam moved to Tuesday" supersedes "exam on Monday").
_CHANGE_WORDS = frozenset({
    "moved", "changed", "change", "changes", "now", "instead", "longer",
    "updated", "update", "corrected", "correction", "rather", "shifted",
    "rescheduled", "postponed", "cancelled", "canceled", "replaced",
})


def _fts_query(text):
    """Sanitize free text into a safe FTS5 AND query. None if no tokens."""
    toks = [t for t in re.findall(r"\w{2,}", (text or "").lower())
            if t not in _STOPWORDS][:12]
    if not toks:
        return None
    return " AND ".join('"%s"' % t.replace('"', "") for t in toks)


def _derive_key(raw):
    """Accept a Fernet key as-is; otherwise derive one from a passphrase.

    Deterministic SHA-256 derivation (no salt to store). Documented v1
    tradeoff: a proper KDF salt would need secure storage; prefer setting
    RON_MEMORY_KEY to a real Fernet key (Fernet.generate_key()).
    """
    raw = (raw or "").strip()
    if not raw:
        raise MemoryStoreError(
            "RON_MEMORY_KEY is not set — refusing to start the memory "
            "store rather than storing plaintext. Set RON_MEMORY_KEY to a "
            "Fernet key and restart.")
    try:
        Fernet(raw.encode())  # validates format
        return raw.encode()
    except Exception:
        digest = hashlib.sha256(raw.encode()).digest()
        return base64.urlsafe_b64encode(digest)


class MemoryStore:
    """SQLite-backed, per-chat isolated, encrypted memory."""

    def __init__(self, path=DB_PATH):
        self._fernet = Fernet(_derive_key(os.environ.get(KEY_ENV)))
        self._lock = threading.RLock()
        try:
            self._db = sqlite3.connect(path, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._init_schema()
        except MemoryStoreError:
            raise
        except Exception as e:
            raise MemoryStoreError(f"cannot open memory DB: {e}")

    # -- schema ---------------------------------------------------------
    def _init_schema(self):
        c = self._db
        c.execute("""CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY,
            chat_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            text_enc BLOB NOT NULL,
            ts REAL NOT NULL)""")
        c.execute("""CREATE INDEX IF NOT EXISTS idx_messages_chat_ts
            ON messages(chat_id, ts)""")
        c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
            USING fts5(text, content='', columnsize=0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS facts(
            id INTEGER PRIMARY KEY,
            chat_id TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'fact',
            text_enc BLOB NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            superseded_by INTEGER,
            created_ts REAL NOT NULL,
            updated_ts REAL NOT NULL)""")
        c.execute("""CREATE INDEX IF NOT EXISTS idx_facts_chat
            ON facts(chat_id)""")
        c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
            USING fts5(text, content='', columnsize=0)""")
        # Consent: P approved sharing fact_id about themself in group chat_id.
        c.execute("""CREATE TABLE IF NOT EXISTS consents(
            chat_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            fact_id INTEGER NOT NULL,
            granted_ts REAL NOT NULL,
            PRIMARY KEY (chat_id, user_id, fact_id))""")
        # Group membership seen by the bot (for "is P in this group?").
        c.execute("""CREATE TABLE IF NOT EXISTS chat_members(
            chat_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            last_seen REAL NOT NULL,
            PRIMARY KEY (chat_id, user_id))""")
        # Private approval requests (DM Yes/No flow).
        c.execute("""CREATE TABLE IF NOT EXISTS pending_approvals(
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            group_chat_id TEXT NOT NULL,
            group_message_id INTEGER,
            target_user_id INTEGER NOT NULL,
            target_name TEXT NOT NULL DEFAULT '',
            fact_id INTEGER,
            dm_chat_id TEXT,
            payload_enc BLOB,
            status TEXT NOT NULL DEFAULT 'pending',
            created_ts REAL NOT NULL,
            expires_ts REAL NOT NULL)""")
        c.execute("""CREATE INDEX IF NOT EXISTS idx_pending_target
            ON pending_approvals(target_user_id, status, expires_ts)""")
        c.commit()

    # -- crypto helpers -------------------------------------------------
    def _enc(self, text):
        return self._fernet.encrypt((text or "").encode("utf-8"))

    def _dec(self, blob):
        try:
            return self._fernet.decrypt(bytes(blob)).decode("utf-8")
        except InvalidToken:
            raise MemoryStoreError(
                "cannot decrypt memory row — wrong RON_MEMORY_KEY?")

    # -- messages -------------------------------------------------------
    def record_message(self, chat_id, user_id, role, text, ts=None):
        """Dual-write target: one row per inbound/outbound message."""
        text = (text or "").strip()
        if not text:
            return None
        ts = ts if ts is not None else time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO messages(chat_id, user_id, role, text_enc, ts)"
                " VALUES(?,?,?,?,?)",
                (str(chat_id), int(user_id or 0), role, self._enc(text), ts))
            mid = cur.lastrowid
            self._db.execute(
                "INSERT INTO messages_fts(rowid, text) VALUES(?,?)",
                (mid, text))
            self._db.commit()
            return mid

    def recall(self, chat_id, query, limit=3):
        """Keyword recall over THIS chat's messages only. Decrypted on read."""
        fq = _fts_query(query)
        if not fq:
            return []
        with self._lock:
            # Two-step on purpose: contentless FTS5 tables do not support
            # being scanned, so never JOIN them — fetch matching rowids first.
            rids = [r[0] for r in self._db.execute(
                "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?",
                (fq,)).fetchall()]
            if not rids:
                return []
            q = ",".join("?" * len(rids))
            rows = self._db.execute(
                "SELECT id, role, text_enc, ts FROM messages"
                f" WHERE id IN ({q}) AND chat_id = ?"
                " ORDER BY ts DESC LIMIT ?",
                (*rids, str(chat_id), int(limit))).fetchall()
        return [{"id": r[0], "role": r[1], "text": self._dec(r[2]), "ts": r[3]}
                for r in rows]

    def _delete_messages(self, ids):
        if not ids:
            return 0
        with self._lock:
            q = ",".join("?" * len(ids))
            rows = self._db.execute(
                f"SELECT id, text_enc FROM messages WHERE id IN ({q})",
                ids).fetchall()
            for rid, enc in rows:
                self._fts_delete("messages_fts", rid, self._dec(enc))
            cur = self._db.execute(
                f"DELETE FROM messages WHERE id IN ({q})", ids)
            self._db.commit()
            return cur.rowcount

    def _fts_delete(self, fts_table, rowid, text):
        """Remove one row from a contentless FTS5 index.

        Plain DELETE is rejected on contentless FTS5 tables; the supported
        form is the 'delete' command with the row's original text.
        """
        self._db.execute(
            f"INSERT INTO {fts_table}({fts_table}, rowid, text)"
            " VALUES('delete', ?, ?)", (int(rowid), text))

    # -- facts ----------------------------------------------------------
    def remember(self, chat_id, fact_text, kind="fact"):
        """Store a distilled fact. Contradicting/dupe handling, no LLM.

        Returns the fact id. A new fact sharing 2+ content words with an
        existing active fact in the same chat (or 1 shared word plus a
        change marker like "moved"/"now") supersedes it (noted, not
        duplicated); an exact duplicate returns the existing id.
        """
        text = (fact_text or "").strip()
        if not text:
            return None
        now = time.time()
        with self._lock:
            existing = self._facts_locked(str(chat_id), include_superseded=False)
            norm = re.sub(r"\s+", " ", text.lower())
            for f in existing:
                if re.sub(r"\s+", " ", f["text"].lower()) == norm:
                    return f["id"]  # already known
            new_words = content_words(text)
            supersede = None
            best = 0
            for f in existing:
                w = content_words(f["text"])
                if not w or not new_words:
                    continue
                shared = w & new_words
                # Deterministic contradiction/update rule: 2+ shared content
                # words, or 1 shared word plus an explicit change marker
                # ("moved", "now", "instead", ...) means the new fact
                # replaces the old one rather than duplicating it.
                if (len(shared) >= 2 or
                        (shared and bool(new_words & _CHANGE_WORDS))):
                    if len(shared) > best:
                        best, supersede = len(shared), f["id"]
            note = ""
            if supersede is not None:
                note = f"replaces fact #{supersede} (contradiction/update)"
                self._db.execute(
                    "UPDATE facts SET updated_ts=? WHERE id=?",
                    (now, supersede))
            cur = self._db.execute(
                "INSERT INTO facts(chat_id, kind, text_enc, note,"
                " created_ts, updated_ts) VALUES(?,?,?,?,?,?)",
                (str(chat_id), kind, self._enc(text), note, now, now))
            fid = cur.lastrowid
            if supersede is not None:
                self._db.execute(
                    "UPDATE facts SET superseded_by=? WHERE id=?",
                    (fid, supersede))
            self._db.execute("INSERT INTO facts_fts(rowid, text) VALUES(?,?)",
                             (fid, text))
            self._db.commit()
            return fid

    def _facts_locked(self, chat_id, include_superseded=False):
        q = ("SELECT id, kind, text_enc, note, superseded_by, created_ts,"
             " updated_ts FROM facts WHERE chat_id=?")
        if not include_superseded:
            q += " AND superseded_by IS NULL"
        q += " ORDER BY updated_ts DESC"
        rows = self._db.execute(q, (str(chat_id),)).fetchall()
        return [{"id": r[0], "kind": r[1], "text": self._dec(r[2]),
                 "note": r[3], "superseded_by": r[4], "chat_id": str(chat_id),
                 "created_ts": r[5], "updated_ts": r[6]} for r in rows]

    def facts(self, chat_id, include_superseded=False):
        with self._lock:
            return self._facts_locked(str(chat_id), include_superseded)

    def get_fact(self, chat_id, fact_id):
        """Fetch one fact by id, enforcing chat scope."""
        with self._lock:
            r = self._db.execute(
                "SELECT id, kind, text_enc, note, superseded_by, created_ts,"
                " updated_ts FROM facts WHERE id=? AND chat_id=?",
                (int(fact_id), str(chat_id))).fetchone()
        if not r:
            return None
        return {"id": r[0], "kind": r[1], "text": self._dec(r[2]),
                "note": r[3], "superseded_by": r[4], "chat_id": str(chat_id),
                "created_ts": r[5], "updated_ts": r[6]}

    def recall_facts(self, chat_id, query=None, limit=5):
        """Active facts for this chat, keyword-ranked when query given."""
        fq = _fts_query(query) if query else None
        with self._lock:
            if fq:
                # Two-step: never JOIN a contentless FTS5 table.
                rids = [r[0] for r in self._db.execute(
                    "SELECT rowid FROM facts_fts WHERE facts_fts MATCH ?",
                    (fq,)).fetchall()]
                if not rids:
                    return []
                q = ",".join("?" * len(rids))
                rows = self._db.execute(
                    "SELECT id, kind, text_enc, note, created_ts, updated_ts"
                    " FROM facts"
                    f" WHERE id IN ({q}) AND chat_id = ?"
                    " AND superseded_by IS NULL"
                    " ORDER BY updated_ts DESC LIMIT ?",
                    (*rids, str(chat_id), int(limit))).fetchall()
                return [{"id": r[0], "kind": r[1], "text": self._dec(r[2]),
                         "note": r[3], "chat_id": str(chat_id),
                         "created_ts": r[4], "updated_ts": r[5]}
                        for r in rows]
            return self._facts_locked(str(chat_id))[:limit]

    def dm_facts(self, user_id):
        """Active facts learned in a DM with this user (chat_id == user_id).

        Used ONLY for consent-gated disclosure matching — never merged into
        another chat's recall.
        """
        with self._lock:
            return self._facts_locked(str(int(user_id or 0)))

    def _delete_facts(self, ids):
        if not ids:
            return 0
        with self._lock:
            q = ",".join("?" * len(ids))
            rows = self._db.execute(
                f"SELECT id, text_enc FROM facts WHERE id IN ({q})",
                ids).fetchall()
            for rid, enc in rows:
                self._fts_delete("facts_fts", rid, self._dec(enc))
            cur = self._db.execute(
                f"DELETE FROM facts WHERE id IN ({q})", ids)
            self._db.commit()
            return cur.rowcount

    def forget(self, chat_id, query):
        """Delete facts + messages in this chat matching query. Returns count."""
        fq = _fts_query(query)
        if not fq:
            return 0
        with self._lock:
            # Two-step: never JOIN a contentless FTS5 table.
            frids = [r[0] for r in self._db.execute(
                "SELECT rowid FROM facts_fts WHERE facts_fts MATCH ?",
                (fq,)).fetchall()]
            mrids = [r[0] for r in self._db.execute(
                "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?",
                (fq,)).fetchall()]
            fids, mids = [], []
            if frids:
                q = ",".join("?" * len(frids))
                fids = [r[0] for r in self._db.execute(
                    f"SELECT id FROM facts WHERE id IN ({q}) AND chat_id=?",
                    (*frids, str(chat_id))).fetchall()]
            if mrids:
                q = ",".join("?" * len(mrids))
                mids = [r[0] for r in self._db.execute(
                    f"SELECT id FROM messages WHERE id IN ({q}) AND chat_id=?",
                    (*mrids, str(chat_id))).fetchall()]
        n = self._delete_facts(fids)
        n += self._delete_messages(mids)
        return n

    def forget_latest_fact(self, chat_id):
        """Delete the most recently updated active fact in this chat."""
        with self._lock:
            r = self._db.execute(
                "SELECT id, text_enc FROM facts"
                " WHERE chat_id=? AND superseded_by IS NULL"
                " ORDER BY updated_ts DESC LIMIT 1",
                (str(chat_id),)).fetchone()
            if not r:
                return None
            self._fts_delete("facts_fts", r[0], self._dec(r[1]))
            self._db.execute("DELETE FROM facts WHERE id=?", (r[0],))
            self._db.commit()
            return r[0]

    def wipe_chat(self, chat_id):
        """Purge everything scoped to this chat. Returns counts."""
        cid = str(chat_id)
        with self._lock:
            mids = [r[0] for r in self._db.execute(
                "SELECT id FROM messages WHERE chat_id=?", (cid,)).fetchall()]
            fids = [r[0] for r in self._db.execute(
                "SELECT id FROM facts WHERE chat_id=?", (cid,)).fetchall()]
            out = {"messages": 0, "facts": 0, "consents": 0,
                   "pendings": 0, "members": 0}
            out["messages"] = self._delete_messages(mids)
            out["facts"] = self._delete_facts(fids)
            out["consents"] = self._db.execute(
                "DELETE FROM consents WHERE chat_id=?", (cid,)).rowcount
            out["pendings"] = self._db.execute(
                "DELETE FROM pending_approvals WHERE group_chat_id=?",
                (cid,)).rowcount
            out["members"] = self._db.execute(
                "DELETE FROM chat_members WHERE chat_id=?", (cid,)).rowcount
            self._db.commit()
            return out

    def prune_expired(self, retention_days=30):
        """Drop raw messages older than retention_days. Facts are kept."""
        cutoff = time.time() - float(retention_days) * 86400
        with self._lock:
            mids = [r[0] for r in self._db.execute(
                "SELECT id FROM messages WHERE ts < ?", (cutoff,)).fetchall()]
        return self._delete_messages(mids)

    # -- consents -------------------------------------------------------
    def has_consent(self, group_chat_id, user_id, fact_id):
        with self._lock:
            r = self._db.execute(
                "SELECT 1 FROM consents WHERE chat_id=? AND user_id=?"
                " AND fact_id=?",
                (str(group_chat_id), int(user_id), int(fact_id))).fetchone()
            return r is not None

    def record_consent(self, group_chat_id, user_id, fact_id):
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO consents(chat_id, user_id, fact_id,"
                " granted_ts) VALUES(?,?,?,?)",
                (str(group_chat_id), int(user_id), int(fact_id), time.time()))
            self._db.commit()

    def consented_facts(self, group_chat_id):
        """Facts P approved for sharing in this group: (user_id, text).

        Read from P's DM scope only via an explicit consent row — the fact
        is never merged into the group's own memory.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT user_id, fact_id FROM consents WHERE chat_id=?",
                (str(group_chat_id),)).fetchall()
        out = []
        for uid, fid in rows:
            f = self.get_fact(str(int(uid)), fid)
            if f and f["superseded_by"] is None:
                out.append((int(uid), f["text"]))
        return out

    # -- membership -----------------------------------------------------
    def note_member(self, chat_id, user_id, name=""):
        with self._lock:
            self._db.execute(
                "INSERT INTO chat_members(chat_id, user_id, name, last_seen)"
                " VALUES(?,?,?,?)"
                " ON CONFLICT(chat_id, user_id) DO UPDATE SET"
                " last_seen=excluded.last_seen,"
                " name=CASE WHEN excluded.name<>'' THEN excluded.name"
                " ELSE name END",
                (str(chat_id), int(user_id), name or "", time.time()))
            self._db.commit()

    def member_ids(self, chat_id):
        with self._lock:
            return [r[0] for r in self._db.execute(
                "SELECT user_id FROM chat_members WHERE chat_id=?",
                (str(chat_id),)).fetchall()]

    def member_name(self, chat_id, user_id):
        with self._lock:
            r = self._db.execute(
                "SELECT name FROM chat_members WHERE chat_id=? AND user_id=?",
                (str(chat_id), int(user_id))).fetchone()
            return (r or [""])[0]

    # -- pending approvals (private DM Yes/No flow) ---------------------
    def create_pending(self, kind, group_chat_id, target_user_id,
                       group_message_id=None, target_name="", fact_id=None,
                       dm_chat_id=None, payload=None, timeout_secs=900):
        """Create a pending approval. payload (e.g. the overshare question
        text) is Fernet-encrypted, not plaintext.

        Privacy note: for overshare approvals we store the question text
        itself (encrypted) rather than just message_id+chat, because the
        Bot API cannot fetch arbitrary old group messages later — without
        the stored text there would be nothing to answer on approval.
        group_message_id is still stored alongside for reply threading.
        """
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO pending_approvals(kind, group_chat_id,"
                " group_message_id, target_user_id, target_name, fact_id,"
                " dm_chat_id, payload_enc, status, created_ts, expires_ts)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (kind, str(group_chat_id), group_message_id,
                 int(target_user_id), target_name or "",
                 fact_id, dm_chat_id,
                 self._enc(payload) if payload else None,
                 "pending", now, now + float(timeout_secs)))
            self._db.commit()
            return cur.lastrowid

    def get_pending(self, pid):
        with self._lock:
            r = self._db.execute(
                "SELECT id, kind, group_chat_id, group_message_id,"
                " target_user_id, target_name, fact_id, dm_chat_id,"
                " payload_enc, status, created_ts, expires_ts"
                " FROM pending_approvals WHERE id=?", (int(pid),)).fetchone()
        if not r:
            return None
        return {"id": r[0], "kind": r[1], "group_chat_id": r[2],
                "group_message_id": r[3], "target_user_id": r[4],
                "target_name": r[5], "fact_id": r[6], "dm_chat_id": r[7],
                "payload": self._dec(r[8]) if r[8] else None,
                "status": r[9], "created_ts": r[10], "expires_ts": r[11]}

    def active_pendings_for(self, target_user_id, group_chat_id=None,
                            kind=None, fact_id=None):
        """Non-expired pending approvals for a user (optionally one group)."""
        now = time.time()
        q = ("SELECT id, kind, group_chat_id, group_message_id,"
             " target_user_id, target_name, fact_id, dm_chat_id,"
             " payload_enc, status, created_ts, expires_ts"
             " FROM pending_approvals"
             " WHERE target_user_id=? AND status='pending' AND expires_ts>?")
        args = [int(target_user_id), now]
        if group_chat_id is not None:
            q += " AND group_chat_id=?"
            args.append(str(group_chat_id))
        if kind is not None:
            q += " AND kind=?"
            args.append(kind)
        if fact_id is not None:
            q += " AND fact_id=?"
            args.append(int(fact_id))
        q += " ORDER BY created_ts DESC"
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        out = []
        for r in rows:
            out.append({"id": r[0], "kind": r[1], "group_chat_id": r[2],
                        "group_message_id": r[3], "target_user_id": r[4],
                        "target_name": r[5], "fact_id": r[6], "dm_chat_id": r[7],
                        "payload": self._dec(r[8]) if r[8] else None,
                        "status": r[9], "created_ts": r[10],
                        "expires_ts": r[11]})
        return out

    def set_pending_status(self, pid, status):
        with self._lock:
            self._db.execute(
                "UPDATE pending_approvals SET status=? WHERE id=?",
                (status, int(pid)))
            self._db.commit()

    def expire_due(self, now=None):
        """Mark overdue pendings expired. Returns their ids."""
        now = now if now is not None else time.time()
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM pending_approvals"
                " WHERE status='pending' AND expires_ts<=?", (now,)).fetchall()
            ids = [r[0] for r in rows]
            if ids:
                q = ",".join("?" * len(ids))
                self._db.execute(
                    f"UPDATE pending_approvals SET status='expired'"
                    f" WHERE id IN ({q})", ids)
                self._db.commit()
            return ids

    def close(self):
        with self._lock:
            try:
                self._db.close()
            except Exception:
                pass
