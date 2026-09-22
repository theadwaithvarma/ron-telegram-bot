#!/usr/bin/env python3
"""Phase 4 — per-chat isolated memory with encryption at rest.

LOCKED DESIGN (Adwaith, 2026-09-20):
- Every row is scoped by Telegram chat_id. Every read filters by chat_id.
  A DM's memory never leaks into a group or another DM. No exceptions.
- Encryption at rest: message text, fact text, and approval payloads are
  encrypted. The key comes from RON_MEMORY_KEY (env). No key ->
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

Track A implementation notes (2026-09-22):
- The `cryptography` package is NOT installed in this environment, so the
  old `from cryptography.fernet import Fernet` import killed the whole
  module (every memory feature dead). Crypto now goes through a clean
  `Cipher` interface (encrypt/decrypt bytes): `StdCipher` (stdlib only,
  see construction below) is the default fallback; `FernetCipher` wraps
  real Fernet when the package is importable. Swapping back is one line:
  set RON_MEMORY_CIPHER=fernet after `pip install cryptography`
  (or pass prefer="fernet" to make_cipher). Blob formats differ between
  the two ciphers (Fernet token vs "RM1" magic header), so a store written
  by one cipher cannot be read by the other — migrating ciphers means
  re-encrypting (export/re-import) or wiping. No live rows exist from the
  Fernet era (the module never imported here), so nothing needs migrating.
- Isolation scheme: ONE SQLite file, chat-scoped tables, every query
  filtered by chat_id. Chosen over per-chat DB files because: a single
  WAL file gives one transaction boundary for cross-table ops (wipe_chat
  touches six tables atomically; consent rows join chat+user+fact);
  per-chat files would multiply inodes/SQLite handles across hundreds of
  groups and complicate backups; one file is one thing to snapshot.
  Enforcement = chat_id on every public read/write + the isolation tests
  in tests/test_memory*.py (write to chat A, read/forget/wipe from chat B,
  assert A untouched). The ONLY sanctioned cross-scope reads are
  dm_facts()/consented_facts(), which match a DM-learned fact to a group
  member SOLELY to ask that person's consent privately via DM — never
  merged into another chat's recall.
- Retention: prune_expired(retention_days) deletes raw messages older
  than retention_days plus superseded (dead-lineage) facts that old;
  active facts persist by default (explicit user knowledge) unless
  fact_retention_days=N is passed. Returns total rows deleted (int).
- New prompt-facing API: record_turn(chat_id, user_id, role, text)
  records one turn side and deterministically extracts durable facts
  from explicit "remember ..." phrasings in user turns (no LLM);
  recall_context(chat_id, query, limit_chars) returns the compact
  context string for the prompt (facts first, then history, char-bounded);
  handle_memory_nl(store, chat_id, user_id, text) is the standalone
  natural-language command dispatcher ("remember this ...", "forget
  that/this/about X", "forget everything"). The live daemon path
  (ron_daemon._handle_memory_nl) adds JSON-history parity around the
  same store calls.
- Logging rule: this module never prints, and no exception message ever
  carries plaintext, key material, or ciphertext. Callers must uphold
  the same rule (log counts and type names only).

Phase 5 — memory lifecycle (2026-09-20):
- Negation-aware contradiction replacement: a negation marker ("not",
  "n't", "never", ...) in the new fact flips meaning, so 1 shared
  content word suffices to supersede. supersession_chain() walks a
  fact's full lineage oldest -> newest, chat-scoped.
- Complete forgetting: forget() expands matched facts to their full
  chains and removes orphan consent / pending-approval rows; returns
  the total deleted (int), with forget_detailed() for the breakdown.
- Richer retrieval: recall_ranked() (FTS5 bm25 relevance, recency
  tiebreak, AND-then-OR fallback) and recall_all() (facts weighted
  above messages, unified ranked list). Still deterministic, zero LLM.

Cross-chat disclosure (refinement, 2026-09-20): matching a fact learned in
a DM with person P to P's Telegram user_id in a group happens ONLY to ask
P's consent PRIVATELY via DM. The fact stays stored under the DM's
chat_id — this is identity matching for consent, NOT memory merging.
Consent is recorded per (group_chat_id, user_id, fact_id): ask once.
"""
import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import struct
import threading
import time

HOME = os.path.expanduser("~")
DB_PATH = os.path.join(HOME, "workspace", "telegram-bot", "ron_memory.db")
KEY_ENV = "RON_MEMORY_KEY"
# One-line cipher swap: "auto" (default) | "fernet" | "stdlib".
CIPHER_ENV = "RON_MEMORY_CIPHER"


class MemoryStoreError(Exception):
    """Raised when the store can't operate safely (no key, bad key, DB error)."""


class CipherError(Exception):
    """Decryption failed: wrong key, tampered blob, or foreign format."""


# ---------------------------------------------------------------------------
# Cipher interface — encrypt/decrypt bytes. StdCipher is the stdlib
# fallback; FernetCipher wraps real Fernet when `cryptography` is
# installed. make_cipher() picks; MemoryStore takes an injected cipher
# for tests.
# ---------------------------------------------------------------------------
class Cipher:
    """Encrypt/decrypt interface (bytes in, bytes out)."""

    def encrypt(self, plaintext: bytes) -> bytes:
        raise NotImplementedError

    def decrypt(self, blob: bytes) -> bytes:
        raise NotImplementedError


class StdCipher(Cipher):
    """Stdlib-only authenticated stream cipher (no `cryptography` needed).

    Construction: master key (32 bytes) is domain-separated with SHA-256
    into k_enc / k_mac. Encryption picks a random 16-byte nonce, builds a
    keystream from HMAC-SHA256(k_enc, nonce || be64(counter)) blocks (CTR
    mode), XORs the plaintext, and appends a 16-byte tag =
    HMAC-SHA256(k_mac, nonce || ciphertext). Blob layout:
        b"RM1" || nonce(16) || ciphertext || tag(16)
    Decryption verifies the tag with hmac.compare_digest before touching
    the keystream, so tampered / wrong-key / foreign blobs all raise
    CipherError without leaking anything about the plaintext.

    LIMITS (read before trusting this with real secrets):
    - DIY construction, NOT an audited primitive. It leans on HMAC-SHA256
      (a solid MAC) used carefully (encrypt-then-MAC, random nonce,
      constant-time tag check), but it has not been reviewed.
    - Reveals plaintext length; no Fernet interoperability (different
      blob format — migrating ciphers needs a re-encrypt or wipe).
    - For production, `pip install cryptography` + RON_MEMORY_CIPHER=fernet
      gets audited AES-128-CBC + HMAC. That is the recommended setup.
    """

    MAGIC = b"RM1"
    NONCE_LEN = 16
    TAG_LEN = 16
    kind = "stdlib"

    def __init__(self, key_material: bytes):
        if not isinstance(key_material, (bytes, bytearray)) \
                or len(key_material) != 32:
            raise CipherError("StdCipher needs 32 bytes of key material")
        km = bytes(key_material)
        self._k_enc = hashlib.sha256(b"ron-mem-enc-v1" + km).digest()
        self._k_mac = hashlib.sha256(b"ron-mem-mac-v1" + km).digest()

    def _keystream(self, nonce: bytes, nbytes: int) -> bytes:
        out = bytearray()
        counter = 0
        while len(out) < nbytes:
            out += hmac.new(self._k_enc,
                            nonce + struct.pack(">Q", counter),
                            hashlib.sha256).digest()
            counter += 1
        return bytes(out[:nbytes])

    def encrypt(self, plaintext: bytes) -> bytes:
        pt = bytes(plaintext)
        nonce = secrets.token_bytes(self.NONCE_LEN)
        ct = bytes(a ^ b for a, b in
                   zip(pt, self._keystream(nonce, len(pt))))
        tag = hmac.new(self._k_mac, nonce + ct,
                       hashlib.sha256).digest()[:self.TAG_LEN]
        return self.MAGIC + nonce + ct + tag

    def decrypt(self, blob: bytes) -> bytes:
        blob = bytes(blob)
        min_len = len(self.MAGIC) + self.NONCE_LEN + self.TAG_LEN
        if len(blob) < min_len or blob[:len(self.MAGIC)] != self.MAGIC:
            raise CipherError("not a stdlib-cipher blob (wrong magic)")
        nonce = blob[3:3 + self.NONCE_LEN]
        tag = blob[-self.TAG_LEN:]
        ct = blob[3 + self.NONCE_LEN:-self.TAG_LEN]
        expect = hmac.new(self._k_mac, nonce + ct,
                          hashlib.sha256).digest()[:self.TAG_LEN]
        if not hmac.compare_digest(tag, expect):
            raise CipherError("authentication failed — wrong key or "
                              "tampered blob")
        ks = self._keystream(nonce, len(ct))
        return bytes(a ^ b for a, b in zip(ct, ks))


class FernetCipher(Cipher):
    """Thin wrapper over cryptography.fernet.Fernet. Raises
    MemoryStoreError at construction when the package is missing."""

    kind = "fernet"

    def __init__(self, fernet_key: bytes):
        try:
            from cryptography.fernet import Fernet, InvalidToken
        except ImportError:
            raise MemoryStoreError(
                "RON_MEMORY_CIPHER=fernet requested but the `cryptography` "
                "package is not installed — pip install cryptography or "
                "unset the variable for the stdlib fallback.")
        self._InvalidToken = InvalidToken
        self._f = Fernet(fernet_key)

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._f.encrypt(bytes(plaintext))

    def decrypt(self, blob: bytes) -> bytes:
        try:
            return self._f.decrypt(bytes(blob))
        except self._InvalidToken:
            raise CipherError("Fernet decryption failed — wrong key or "
                              "tampered token")


def generate_key():
    """New random key, Fernet-compatible format (urlsafe-b64, 32 bytes).

    Works for both ciphers: FernetCipher takes it as-is, StdCipher uses
    the decoded 32 bytes as key material. Store in RON_MEMORY_KEY."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def has_cryptography():
    """True when `cryptography` is importable (FernetCipher usable)."""
    try:
        import cryptography.fernet  # noqa: F401
        return True
    except ImportError:
        return False


def _derive_key(raw):
    """Fernet-key bytes from RON_MEMORY_KEY (backward compatible).

    A real Fernet key passes through byte-identical (ron_backup._fernet
    feeds this straight into Fernet(), so this contract is frozen).
    Anything else is treated as a passphrase and stretched with
    PBKDF2-HMAC-SHA256 (600k iterations). The salt is a fixed app string:
    documented v1 tradeoff — there is no secure salt store yet, and the
    salt is not secret, it only needs to be unique per application. A
    generated key (generate_key()) skips the KDF entirely and is
    strictly preferred over a human passphrase.
    """
    raw = (raw or "").strip()
    if not raw:
        raise MemoryStoreError(
            "RON_MEMORY_KEY is not set — refusing to start the memory "
            "store rather than storing plaintext. Set RON_MEMORY_KEY to a "
            "Fernet key and restart.")
    try:
        key = raw.encode("ascii")
        if len(base64.urlsafe_b64decode(key)) == 32:
            return key
    except Exception:
        pass
    dk = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"),
                             b"ron-memory-kdf-v1", 600_000, dklen=32)
    return base64.urlsafe_b64encode(dk)


def make_cipher(key_raw=None, prefer=None):
    """Build the cipher for a MemoryStore. Fail closed: no key -> raise.

    prefer: "auto" (default: Fernet when importable, else stdlib),
    "fernet" (require cryptography), or "stdlib" (force stdlib).
    Also honors the RON_MEMORY_CIPHER env var. The auto->stdlib fallback
    prints one config line (no key material, no content).
    """
    if key_raw is None:
        key_raw = os.environ.get(KEY_ENV)
    prefer = (prefer or os.environ.get(CIPHER_ENV, "") or "auto")
    prefer = prefer.strip().lower()
    if prefer not in ("auto", "fernet", "stdlib"):
        raise MemoryStoreError(
            f"bad {CIPHER_ENV}={prefer!r}: want auto|fernet|stdlib")
    fkey = _derive_key(key_raw)  # raises when the key is missing: fail closed
    material = base64.urlsafe_b64decode(fkey)
    if prefer == "stdlib":
        return StdCipher(material)
    try:
        return FernetCipher(fkey)
    except MemoryStoreError:
        if prefer == "fernet":
            raise
        print("[ron_memory] `cryptography` not installed — using the "
              "stdlib cipher fallback (set RON_MEMORY_CIPHER=fernet after "
              "pip install cryptography for audited Fernet)",
              flush=True)
        return StdCipher(material)


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

# Phase 5: negation markers. A negation flips a fact's meaning ("exam is
# Monday" -> "exam is not Monday"), so when the NEW fact carries one, a
# single shared content word is enough to link it to the old fact —
# weaker overlap suffices because the flip itself is the signal.
_NEGATION_WORDS = frozenset({
    "not", "no", "never", "no longer", "stopped", "isn't", "wasn't",
    "aren't", "don't", "doesn't", "didn't", "won't", "can't", "cannot",
})


def _has_negation(text):
    """True if the new fact contains a negation marker.

    Checks the raw text (not content words): most markers ("not", "no",
    "n't") are stopwords or too short to survive content_words(), but
    they still flip meaning, so they must be detected separately.
    """
    t = (text or "").lower()
    if "n't" in t:  # isn't, wasn't, aren't, don't, doesn't, didn't, ...
        return True
    if "no longer" in t:
        return True
    return bool(re.search(r"\b(not|no|never|stopped|cannot)\b", t))


def _fts_query(text):
    """Sanitize free text into a safe FTS5 AND query. None if no tokens."""
    toks = [t for t in re.findall(r"\w{2,}", (text or "").lower())
            if t not in _STOPWORDS][:12]
    if not toks:
        return None
    return " AND ".join('"%s"' % t.replace('"', "") for t in toks)


# ---------------------------------------------------------------------------
# Track A: deterministic v1 fact extraction. Explicit "remember ..."
# phrasings only — no LLM, no guessing at implicit facts (that's v2).
# ---------------------------------------------------------------------------
_EXTRACT_RES = [
    re.compile(r"\bremember\s+(?:that\s+)?(.{4,220})", re.I | re.S),
    re.compile(r"\bnote\s+down\s+(?:that\s+)?(.{4,220})", re.I | re.S),
    re.compile(r"\bdon'?t\s+forget\s+(?:that\s+)?(.{4,220})", re.I | re.S),
]
_EXTRACT_LEAD_RE = re.compile(r"^(this|that)\s*:\s*", re.I)


def extract_fact_statements(text):
    """Fact strings explicitly asked to be remembered. [] when none.

    Pure function (no store) so it unit-tests standalone. A leading
    "this:"/"that:" is stripped ("remember this: my dog is Max" ->
    "my dog is Max"). Duplicates collapse, order preserved.
    """
    out = []
    for rx in _EXTRACT_RES:
        for m in rx.finditer(text or ""):
            fact = _EXTRACT_LEAD_RE.sub(
                "", re.sub(r"\s+", " ", m.group(1)).strip())
            fact = fact.strip().strip(" .!?;:")
            if len(fact) >= 4:
                out.append(fact)
    seen, uniq = set(), []
    for f in out:
        if f.lower() not in seen:
            seen.add(f.lower())
            uniq.append(f)
    return uniq


_SQLITE_MAX_VARS = 30000  # under SQLite's 32766 host-parameter ceiling
_FTS_ROWID_CAP = 5000  # max FTS rowid candidates pulled for one recall query


def _in_chunks(db, sql, ids, extra_params=(), fetch=True):
    """Run `sql` (one {} placeholder for the IN-list) over `ids` in chunks.

    A huge id list in an IN (...) clause would exceed SQLite's bound
    variable limit and raise; chunking keeps every statement under it.
    Chunks concatenate fetchall() rows; for write statements pass
    fetch=False and the rowcounts are summed instead.
    """
    ids = list(ids)
    if not ids:
        return [] if fetch else 0
    out = [] if fetch else 0
    step = max(1, _SQLITE_MAX_VARS - len(extra_params) - 1)
    for i in range(0, len(ids), step):
        chunk = ids[i:i + step]
        q = ",".join("?" * len(chunk))
        cur = db.execute(sql.format(q=q), (*chunk, *extra_params))
        if fetch:
            out.extend(cur.fetchall())
        else:
            out += cur.rowcount
    return out


class MemoryStore:
    """SQLite-backed, per-chat isolated, encrypted memory."""

    def __init__(self, path=DB_PATH, cipher=None):
        # Fail closed: make_cipher raises MemoryStoreError when
        # RON_MEMORY_KEY is missing — the store never runs on plaintext.
        self._cipher = cipher if cipher is not None else make_cipher()
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

    @property
    def cipher_kind(self):
        """'fernet' or 'stdlib' — which cipher this store encrypts with."""
        return getattr(self._cipher, "kind", "?")

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
        # Phase 6 (2026-09-20): generic per-chat key/value store —
        # rolling history summaries live here, encrypted at rest, never
        # in a new plaintext store. wipe_chat() nukes these rows too.
        c.execute("""CREATE TABLE IF NOT EXISTS chat_meta(
            chat_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value_enc BLOB NOT NULL,
            updated_ts REAL NOT NULL,
            PRIMARY KEY(chat_id, key))""")
        c.commit()

    # -- crypto helpers -------------------------------------------------
    def _enc(self, text):
        return self._cipher.encrypt((text or "").encode("utf-8"))

    def _dec(self, blob):
        try:
            return self._cipher.decrypt(bytes(blob)).decode("utf-8")
        except CipherError:
            # Never hint at key material or content — generic message only.
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

    def record_turn(self, chat_id, user_id, role, text, ts=None):
        """Record one turn side and extract durable facts from user turns.

        Track A API: records the message row, then runs deterministic v1
        fact extraction (explicit "remember ..." phrasings only, no LLM)
        on USER turns — assistant turns never yield facts. Returns
        {"message_id": int|None, "fact_ids": [int]}. remember() dedupes,
        so re-recording the same statement returns the existing fact id.
        """
        mid = self.record_message(chat_id, user_id, role, text, ts=ts)
        fids = []
        if mid is not None and role == "user":
            for fact in extract_fact_statements(text):
                fid = self.remember(chat_id, fact)
                if fid is not None:
                    fids.append(fid)
        return {"message_id": mid, "fact_ids": fids}

    def recall(self, chat_id, query, limit=3):
        """Keyword recall over THIS chat's messages only. Decrypted on read."""
        fq = _fts_query(query)
        if not fq:
            return []
        with self._lock:
            # Two-step on purpose: contentless FTS5 tables do not support
            # being scanned, so never JOIN them — fetch matching rowids first.
            rids = [r[0] for r in self._db.execute(
                "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?"
                " ORDER BY rowid DESC LIMIT ?",
                (fq, _FTS_ROWID_CAP)).fetchall()]
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

    def _bm25_message_hits(self, cid, fq):
        """(rowid -> bm25) candidates for one FTS query, chat-scoped.

        Two-step on purpose: contentless FTS5 tables do not support being
        scanned, so never JOIN them — pull (rowid, bm25) pairs from the
        index first, then fetch the real rows by id.
        """
        pairs = self._db.execute(
            "SELECT rowid, bm25(messages_fts) FROM messages_fts"
            " WHERE messages_fts MATCH ?"
            " ORDER BY bm25(messages_fts) LIMIT ?",
            (fq, _FTS_ROWID_CAP)).fetchall()
        if not pairs:
            return []
        score = {r: s for r, s in pairs}
        q = ",".join("?" * len(score))
        rows = self._db.execute(
            "SELECT id, role, text_enc, ts FROM messages"
            f" WHERE id IN ({q}) AND chat_id=?",
            (*score.keys(), cid)).fetchall()
        out = [{"id": r[0], "role": r[1], "text": self._dec(r[2]),
                "ts": r[3], "_bm25": score[r[0]]} for r in rows]
        # Relevance dominates: bm25() is negative, more negative = better
        # match, so ascending bm25 wins. Recency only breaks exact ties
        # (e.g. two identical messages) — an older relevant message
        # always outranks a newer irrelevant one.
        out.sort(key=lambda h: (h["_bm25"], -h["ts"]))
        return out

    def recall_ranked(self, chat_id, query, limit=5):
        """Keyword recall over this chat's messages, relevance-ranked.

        FTS5 bm25() relevance first, recency as tiebreak. Tries an AND
        query over the tokens first; if nothing matches, retries with OR
        over the top tokens so partial matches still surface. Decrypted
        on read, chat-scoped, bounded. Zero LLM calls.
        """
        cid = str(chat_id)
        toks = [t for t in re.findall(r"\w{2,}", (query or "").lower())
                if t not in _STOPWORDS][:12]
        if not toks:
            return []
        and_q = " AND ".join('"%s"' % t.replace('"', "") for t in toks)
        with self._lock:
            hits = self._bm25_message_hits(cid, and_q)
            if not hits:
                # AND was too strict (no single message has every term):
                # fall back to OR so the best partial matches surface.
                or_q = " OR ".join('"%s"' % t.replace('"', "")
                                   for t in toks[:6])
                hits = self._bm25_message_hits(cid, or_q)
        return [{"id": h["id"], "role": h["role"], "text": h["text"],
                 "ts": h["ts"]} for h in hits[:max(int(limit), 0)]]

    def _delete_messages(self, ids):
        if not ids:
            return 0
        with self._lock:
            rows = _in_chunks(
                self._db, "SELECT id, text_enc FROM messages WHERE id IN ({q})",
                ids)
            for rid, enc in rows:
                self._fts_delete("messages_fts", rid, self._dec(enc))
            total = _in_chunks(
                self._db, "DELETE FROM messages WHERE id IN ({q})",
                ids, fetch=False)
            self._db.commit()
            return total

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
            negated = _has_negation(text)
            supersede = None
            supersede_kind = ""
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
                # Phase 5: a negation marker in the NEW fact ("not", "n't",
                # "never", ...) flips meaning, so 1 shared content word is
                # enough to confidently link and replace the old fact.
                if len(shared) >= 2:
                    rule, strength = "contradiction/update", len(shared)
                elif shared and (new_words & _CHANGE_WORDS):
                    rule, strength = "contradiction/update", len(shared)
                elif shared and negated:
                    rule, strength = "negation", len(shared)
                else:
                    continue
                if strength > best:
                    best, supersede, supersede_kind = strength, f["id"], rule
            note = ""
            if supersede is not None:
                note = f"replaces fact #{supersede} ({supersede_kind})"
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

    def supersession_chain(self, chat_id, fact_id):
        """Full supersession chain containing fact_id, oldest -> newest.

        Follows superseded_by links in both directions (ancestors that
        this fact replaced, descendants that replaced it). Chat-scoped:
        the walk starts only if fact_id belongs to this chat, and every
        link is resolved within the same chat — a foreign fact id yields
        an empty chain, never a cross-chat walk.
        """
        cid = str(chat_id)
        with self._lock:
            start = self._db.execute(
                "SELECT id FROM facts WHERE id=? AND chat_id=?",
                (int(fact_id), cid)).fetchone()
            if not start:
                return []
            # successor map: old fact id -> newer fact id that replaced it.
            rows = self._db.execute(
                "SELECT id, superseded_by FROM facts WHERE chat_id=?",
                (cid,)).fetchall()
        succ = {r[0]: r[1] for r in rows if r[1] is not None}
        # predecessor map: newer fact id -> old fact id it replaced.
        # remember() only ever builds linear chains, but setdefault keeps
        # this safe even if the data ever branches.
        pred = {}
        for fid, sid in succ.items():
            pred.setdefault(sid, fid)
        # Walk back to the oldest ancestor, then forward to the newest.
        cur = int(fact_id)
        while cur in pred:
            cur = pred[cur]
        ids = []
        while cur is not None:
            ids.append(cur)
            cur = succ.get(cur)
        with self._lock:
            rmap = {r[0]: r for r in _in_chunks(
                self._db,
                "SELECT id, kind, text_enc, note, superseded_by,"
                " created_ts, updated_ts FROM facts WHERE id IN ({q})",
                ids)}
        return [{"id": r[0], "kind": r[1], "text": self._dec(r[2]),
                 "note": r[3], "superseded_by": r[4], "chat_id": cid,
                 "created_ts": r[5], "updated_ts": r[6]}
                for r in (rmap[i] for i in ids)]

    def recall_facts(self, chat_id, query=None, limit=5):
        """Active facts for this chat, keyword-ranked when query given."""
        fq = _fts_query(query) if query else None
        with self._lock:
            if fq:
                # Two-step: never JOIN a contentless FTS5 table.
                rids = [r[0] for r in self._db.execute(
                    "SELECT rowid FROM facts_fts WHERE facts_fts MATCH ?"
                    " ORDER BY rowid DESC LIMIT ?",
                    (fq, _FTS_ROWID_CAP)).fetchall()]
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

    def recall_all(self, chat_id, query, limit=5):
        """Unified facts + messages in one ranked list for this chat.

        Facts are weighted above messages (distilled signal beats raw
        history), then relevance-ranked messages fill the rest. Every
        item carries "kind": "fact" or "message" (the row type — this
        overwrites the fact's own kind field, always 'fact' in practice).
        Chat-scoped, bounded to `limit` items total, zero LLM calls.
        """
        cid = str(chat_id)
        limit = max(int(limit), 0)
        toks = [t for t in re.findall(r"\w{2,}", (query or "").lower())
                if t not in _STOPWORDS]
        facts = self.recall_facts(cid, query if toks else None, limit=limit)
        msgs = self.recall_ranked(cid, query, limit=limit) if toks else []
        items = [{**f, "kind": "fact"} for f in facts]
        items += [{**m, "kind": "message"} for m in msgs]
        return items[:limit]

    def recall_context(self, chat_id, query, limit_chars=700, limit=3):
        """Compact prompt-ready context string for this chat. "" when none.

        Track A API: one ranked pass (facts above messages), each item on
        its own line, the whole string hard-bounded to limit_chars so the
        prompt never gets a transcript dump. Chat-scoped, zero LLM calls.
        """
        bits = []
        for item in self.recall_all(str(chat_id), query,
                                    limit=max(int(limit), 0)):
            if item["kind"] == "fact":
                bits.append("- " + item["text"])
            else:
                bits.append("- (earlier) " + item["text"][:160])
        if not bits:
            return ""
        return ("\nRelevant memory from this chat:\n"
                + "\n".join(bits))[:max(int(limit_chars), 0)]

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
            rows = _in_chunks(
                self._db, "SELECT id, text_enc FROM facts WHERE id IN ({q})",
                ids)
            for rid, enc in rows:
                self._fts_delete("facts_fts", rid, self._dec(enc))
            total = _in_chunks(
                self._db, "DELETE FROM facts WHERE id IN ({q})",
                ids, fetch=False)
            self._db.commit()
            return total

    def forget_detailed(self, chat_id, query):
        """Forget with a breakdown: {"facts", "messages", "consents",
        "pendings"} counts.

        Every FTS-matched fact expands to its FULL supersession chain
        (ancestors + descendants) so no stale version survives, and any
        consent / pending-approval rows pointing at a deleted fact are
        removed too — orphan rows would break consented_facts lookups
        and re-ask approvals for facts that no longer exist.
        """
        fq = _fts_query(query)
        zero = {"facts": 0, "messages": 0, "consents": 0, "pendings": 0}
        if not fq:
            return dict(zero)
        cid = str(chat_id)
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
                fids = [r[0] for r in _in_chunks(
                    self._db,
                    "SELECT id FROM facts WHERE id IN ({q}) AND chat_id=?",
                    frids, (cid,))]
            if mrids:
                mids = [r[0] for r in _in_chunks(
                    self._db,
                    "SELECT id FROM messages WHERE id IN ({q}) AND chat_id=?",
                    mrids, (cid,))]
            # Expand each matched fact to its full chain: forgetting one
            # version forgets the whole lineage (old contradictions and
            # newer replacements alike).
            chain_ids = set()
            for fid in fids:
                for f in self.supersession_chain(cid, fid):
                    chain_ids.add(f["id"])
            fids = sorted(chain_ids)
        out = dict(zero)
        out["facts"] = self._delete_facts(fids)
        out["messages"] = self._delete_messages(mids)
        if fids:
            with self._lock:
                out["consents"] = _in_chunks(
                    self._db, "DELETE FROM consents WHERE fact_id IN ({q})",
                    fids, fetch=False)
                out["pendings"] = _in_chunks(
                    self._db,
                    "DELETE FROM pending_approvals WHERE fact_id IN ({q})",
                    fids, fetch=False)
                self._db.commit()
        return out

    def forget(self, chat_id, query):
        """Delete facts + messages in this chat matching query.

        Matched facts expand to their full supersession chains; consent
        and pending-approval rows for deleted facts go too. Returns the
        total rows deleted as an int (daemon call site compatible).
        """
        return sum(self.forget_detailed(chat_id, query).values())

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
                   "pendings": 0, "members": 0, "meta": 0}
            out["messages"] = self._delete_messages(mids)
            out["facts"] = self._delete_facts(fids)
            out["consents"] = self._db.execute(
                "DELETE FROM consents WHERE chat_id=?", (cid,)).rowcount
            out["pendings"] = self._db.execute(
                "DELETE FROM pending_approvals WHERE group_chat_id=?",
                (cid,)).rowcount
            out["members"] = self._db.execute(
                "DELETE FROM chat_members WHERE chat_id=?", (cid,)).rowcount
            out["meta"] = self._db.execute(
                "DELETE FROM chat_meta WHERE chat_id=?", (cid,)).rowcount
            self._db.commit()
            return out

    def wipe_user(self, user_id):
        """Purge everything attributable to one user id. Returns counts.

        This covers BOTH directions:
        1. Rows carrying the user's id anywhere (their turns in DMs,
           groups and Business chats; their consents; pending approvals
           targeting them; group memberships).
        2. The user's private-DM chat (chat_id == str(user_id)) as a whole:
           facts, chat_meta (summaries), and the bot's own turns there are
           all part of that private conversation and go too.

        Facts in OTHER chats are chat-scoped (no user attribution) and are
        NOT touched — use wipe_chat()/forget() for those. Returns
        {messages, facts, consents, pendings, members, meta}.
        """
        uid = int(user_id)
        cid = str(uid)
        out = {"messages": 0, "facts": 0, "consents": 0, "pendings": 0,
               "members": 0, "meta": 0}
        with self._lock:
            # 1. The private-DM chat: nuke it like wipe_chat() does.
            mids = [r[0] for r in self._db.execute(
                "SELECT id FROM messages WHERE chat_id=?", (cid,)).fetchall()]
            fids = [r[0] for r in self._db.execute(
                "SELECT id FROM facts WHERE chat_id=?", (cid,)).fetchall()]
            out["messages"] += self._delete_messages(mids)
            out["facts"] = self._delete_facts(fids)
            out["meta"] = self._db.execute(
                "DELETE FROM chat_meta WHERE chat_id=?", (cid,)).rowcount
            out["members"] += self._db.execute(
                "DELETE FROM chat_members WHERE chat_id=?", (cid,)).rowcount
            out["consents"] += self._db.execute(
                "DELETE FROM consents WHERE chat_id=?", (cid,)).rowcount
            out["pendings"] += self._db.execute(
                "DELETE FROM pending_approvals WHERE dm_chat_id=?",
                (cid,)).rowcount
            # 2. The user's turns anywhere else (groups, Business chats).
            mids = [r[0] for r in self._db.execute(
                "SELECT id FROM messages WHERE user_id=?",
                (uid,)).fetchall()]
            out["messages"] += self._delete_messages(mids)
            out["consents"] += self._db.execute(
                "DELETE FROM consents WHERE user_id=?", (uid,)).rowcount
            out["pendings"] += self._db.execute(
                "DELETE FROM pending_approvals WHERE target_user_id=?",
                (uid,)).rowcount
            out["members"] += self._db.execute(
                "DELETE FROM chat_members WHERE user_id=?",
                (uid,)).rowcount
            # Orphan cleanup: consent / approval rows that pointed at the
            # wiped DM facts.
            if fids:
                out["consents"] += _in_chunks(
                    self._db, "DELETE FROM consents WHERE fact_id IN ({q})",
                    fids, fetch=False)
                out["pendings"] += _in_chunks(
                    self._db,
                    "DELETE FROM pending_approvals WHERE fact_id IN ({q})",
                    fids, fetch=False)
            self._db.commit()
            return out

    def prune_expired(self, retention_days=30, fact_retention_days=None):
        """Expire old rows per the retention policy. Returns total deleted.

        - Raw messages older than retention_days are always deleted.
        - Superseded (dead-lineage) facts older than retention_days go too.
        - Active facts persist by default — they are explicit user
          knowledge ("remember this ..."), not raw history. Pass
          fact_retention_days=N to also expire active facts whose
          updated_ts is older than N days.
        The daemon's approval sweeper calls prune_expired(days) daily.
        """
        cutoff = time.time() - float(retention_days) * 86400
        with self._lock:
            mids = [r[0] for r in self._db.execute(
                "SELECT id FROM messages WHERE ts < ?", (cutoff,)).fetchall()]
            fids = [r[0] for r in self._db.execute(
                "SELECT id FROM facts WHERE updated_ts < ?"
                " AND superseded_by IS NOT NULL", (cutoff,)).fetchall()]
            if fact_retention_days is not None:
                fcut = time.time() - float(fact_retention_days) * 86400
                fids += [r[0] for r in self._db.execute(
                    "SELECT id FROM facts WHERE updated_ts < ?"
                    " AND superseded_by IS NULL", (fcut,)).fetchall()]
        n = self._delete_messages(mids)
        n += self._delete_facts(fids)
        if fids:
            # Orphan cleanup, same as forget_detailed().
            with self._lock:
                _in_chunks(
                    self._db, "DELETE FROM consents WHERE fact_id IN ({q})",
                    fids, fetch=False)
                _in_chunks(
                    self._db,
                    "DELETE FROM pending_approvals WHERE fact_id IN ({q})",
                    fids, fetch=False)
                self._db.commit()
        return n

    # -- chat_meta (Phase 6 rolling history summaries) ------------------
    def get_meta(self, chat_id, key):
        """One encrypted value for this chat. None when absent."""
        with self._lock:
            r = self._db.execute(
                "SELECT value_enc FROM chat_meta WHERE chat_id=? AND key=?",
                (str(chat_id), str(key))).fetchone()
        if not r:
            return None
        return self._dec(r[0])

    def set_meta(self, chat_id, key, value):
        """Store/overwrite one encrypted value for this chat."""
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO chat_meta(chat_id, key, value_enc,"
                " updated_ts) VALUES(?,?,?,?)",
                (str(chat_id), str(key), self._enc(value), time.time()))
            self._db.commit()

    def count_summaries(self):
        """Number of chats holding a rolling history summary. Phase 7.

        Counts rows only — summary content is never read. Best-effort:
        returns 0 (not raises) if the table is missing or the DB errors,
        so a status command can never break on this.
        """
        try:
            with self._lock:
                r = self._db.execute(
                    "SELECT COUNT(DISTINCT chat_id) FROM chat_meta "
                    "WHERE key='summary'").fetchone()
            return int(r[0]) if r else 0
        except Exception:
            return 0

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
        text) is encrypted, not plaintext.

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
                _in_chunks(
                    self._db,
                    "UPDATE pending_approvals SET status='expired'"
                    " WHERE id IN ({q})",
                    ids, fetch=False)
                self._db.commit()
            return ids

    def close(self):
        with self._lock:
            try:
                self._db.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Track A: natural-language memory commands, standalone dispatcher.
# ---------------------------------------------------------------------------
def handle_memory_nl(store, chat_id, user_id, text):
    """'remember this ...' / 'forget ...' / 'forget everything' against a
    MemoryStore. Returns (handled: bool, reply: str).

    Standalone reference for integrators. The live daemon path
    (ron_daemon._handle_memory_nl) wraps the same store calls and adds
    JSON-history parity plus non-AI-turn dual-write — it stays the
    production call site.
    """
    try:
        # Same module family; lazy so ron_memory stays importable alone.
        from ron_privacy import parse_remember, parse_forget
    except Exception:
        return False, ""
    fact = parse_remember(text)
    if fact is not None:
        store.remember(chat_id, fact)
        return True, "Noted \u2713 \u2014 I'll remember that for this chat."
    parsed = parse_forget(text)
    if parsed is None:
        return False, ""
    kind, q = parsed
    if kind == "all":
        store.wipe_chat(chat_id)
        return True, "Wiped this chat's memory \u2713 \u2014 starting fresh."
    if kind == "query":
        n = store.forget(chat_id, q)
        return True, "Forgotten \u2713" if n else "Nothing matching to forget."
    fid = store.forget_latest_fact(chat_id)
    return True, ("Forgotten \u2713" if fid
                  else "There's nothing stored to forget.")
