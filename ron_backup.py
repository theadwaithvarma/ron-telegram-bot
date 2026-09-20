#!/usr/bin/env python3
"""Phase 7 (2026-09-20) — nightly encrypted SQLite snapshots.

Takes a consistent snapshot of the bot's state (memory DB, cache DB, and
key JSON state files), bundles it into a tar.gz, encrypts it with the same
RON_MEMORY_KEY the memory DB uses (Fernet), and writes a timestamped file
into backups/. Keeps the last 7 snapshots, prunes older ones.

Key handling:
- Read from the RON_MEMORY_KEY environment variable first (same source the
  daemon uses), else parsed out of the systemd override that feeds the
  daemon (/etc/systemd/system/ron-watchdog.service.d/override.conf).
- The key is NEVER printed, logged, or included in any error/status text.
- No key -> loud failure in logs, non-zero exit, and NO unencrypted backup
  is ever written (fail closed, like the memory store).

Daemon safety: SQLite files are copied with the online backup API
(sqlite3.Connection.backup), which takes a consistent snapshot without
blocking the live daemon's writers. JSON state files in this repo are
written atomically (tmp+rename), so a plain read is safe. After writing,
the bundle is decrypted and integrity-checked before the run is marked
good — a corrupt snapshot never counts.

Intended to run from cron, ~03:00 local:
    30 21 * * * HOME=/home/hatch /usr/bin/python3 \
        /home/hatch/workspace/telegram-bot/ron_backup.py \
        >> /home/hatch/workspace/telegram-bot/ron_backup.log 2>&1
(21:30 UTC == 03:00 IST. Needs no network: purely local files + Fernet.)

Run: python3 ron_backup.py   (exit 0 = good snapshot, 1 = failure, see log)
"""
import io
import json
import os
import re
import sqlite3
import sys
import tarfile
import tempfile
import time

from cryptography.fernet import Fernet, InvalidToken

HOME = os.path.expanduser("~")
BASE_DIR = os.path.join(HOME, "workspace", "telegram-bot")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
STATUS_FILE = os.path.join(BASE_DIR, "ron_backup.json")
KEY_ENV = "RON_MEMORY_KEY"
# The file that feeds the daemon its environment. Parsed with a regex —
# never executed — for the Environment=RON_MEMORY_KEY=... line.
OVERRIDE_PATH = "/etc/systemd/system/ron-watchdog.service.d/override.conf"
KEEP = 7  # daily snapshots retained

# The memory DB is mandatory (the whole point of the backup); everything
# else is best-effort — a missing cache DB must not fail the run.
SQLITE_MANDATORY = ("ron_memory.db",)
SQLITE_OPTIONAL = ("ron_cache.db",)
JSON_FILES = ("ron_ai_history.json", "ron_cache_stats.json", "ron_ai.json")


class BackupError(Exception):
    """Backup failed. Messages never contain key material."""


def _log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    print(f"[ron_backup] {ts}Z {msg}", flush=True)


def _read_key(explicit=None):
    """Return the raw key string from env or the systemd override.

    Raises BackupError (no key material in the message) when neither
    source has it — the caller must then fail WITHOUT writing a backup.
    """
    if explicit:
        return explicit
    env = os.environ.get(KEY_ENV)
    if env:
        return env
    try:
        with open(OVERRIDE_PATH, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        content = ""
    # Try quoted values first (a passphrase key may contain spaces — a
    # bare [^\s] capture would silently truncate it and encrypt under the
    # wrong key, producing unrestorable "ok" snapshots), then the bare
    # token form. Matches:
    #   Environment=RON_MEMORY_KEY=<token>
    #   Environment="RON_MEMORY_KEY=<anything but a quote>"
    #   Environment='RON_MEMORY_KEY=<anything but a quote>'
    m = re.search(r'(?m)^[ \t]*Environment[ \t]*=[ \t]*"'
                  + re.escape(KEY_ENV) + r'=([^"]*)"', content)
    if not m:
        m = re.search(r"(?m)^[ \t]*Environment[ \t]*=[ \t]*'"
                      + re.escape(KEY_ENV) + r"=([^']*)'", content)
    if not m:
        m = re.search(r'(?m)^[ \t]*Environment[ \t]*=[ \t]*'
                      + re.escape(KEY_ENV) + r'=([^\s"\']+)', content)
    if m and m.group(1):
        return m.group(1)
    raise BackupError(
        f"{KEY_ENV} not found in environment or {OVERRIDE_PATH} — "
        "refusing to write an unencrypted backup.")


def _fernet(key_raw):
    """Build the Fernet instance with the memory store's key derivation.

    Imported from ron_memory so the derivation can never drift from the
    one the live DB uses.
    """
    _self_dir = os.path.dirname(os.path.abspath(__file__))
    if _self_dir not in sys.path:
        sys.path.insert(0, _self_dir)
    from ron_memory import _derive_key
    return Fernet(_derive_key(key_raw))


def _snapshot_sqlite(src_path, dst_path):
    """Consistent copy of a possibly-live SQLite DB via the backup API.

    The source is opened read-only: the backup API only reads, and a
    read-only open can never take a write lock against the live daemon.
    """
    from pathlib import Path
    src_uri = Path(src_path).as_uri() + "?mode=ro"
    src = sqlite3.connect(src_uri, uri=True, timeout=30)
    try:
        dst = sqlite3.connect(dst_path, timeout=30)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _stage(base_dir, stage_dir):
    """Copy snapshot sources into stage_dir. Returns list of staged names."""
    staged = []
    for name in SQLITE_MANDATORY:
        src = os.path.join(base_dir, name)
        if not os.path.exists(src):
            raise BackupError(f"mandatory source missing: {name}")
        _snapshot_sqlite(src, os.path.join(stage_dir, name))
        staged.append(name)
    for name in SQLITE_OPTIONAL:
        src = os.path.join(base_dir, name)
        if not os.path.exists(src):
            _log(f"optional source missing, skipping: {name}")
            continue
        try:
            _snapshot_sqlite(src, os.path.join(stage_dir, name))
        except Exception as e:
            _log(f"optional source failed, skipping: {name} ({e})")
            continue
        staged.append(name)
    for name in JSON_FILES:
        src = os.path.join(base_dir, name)
        if not os.path.exists(src):
            _log(f"state file missing, skipping: {name}")
            continue
        with open(src, "rb") as f:
            data = f.read()
        # Validate it's actually JSON before trusting it in the bundle.
        json.loads(data.decode("utf-8"))
        with open(os.path.join(stage_dir, name), "wb") as f:
            f.write(data)
        staged.append(name)
    if not staged:
        raise BackupError("nothing staged — no sources found")
    return staged


def _bundle(stage_dir, staged):
    """Tar.gz the staged files. Returns bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in staged:
            path = os.path.join(stage_dir, name)
            ti = tf.gettarinfo(path, arcname=name)
            ti.mtime = int(time.time())
            with open(path, "rb") as f:
                tf.addfile(ti, f)
    return buf.getvalue()


def _verify(bundle_bytes, fernet, staged):
    """Decrypt + extract to a temp dir, integrity-check every member.

    Raises BackupError on any problem (tamper, truncation, corruption).
    """
    try:
        raw = fernet.decrypt(bundle_bytes)
    except InvalidToken:
        raise BackupError("verification failed: bundle does not decrypt "
                          "(wrong key or tampered file)")
    tmp = tempfile.mkdtemp(prefix="ron_backup_verify_")
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            members = tf.getmembers()
            for m in members:
                # The bundle is written by us, but verify defensively:
                # no absolute paths, no directory traversal.
                if m.name.startswith("/") or ".." in m.name.split("/"):
                    raise BackupError("verification failed: unsafe member "
                                      f"name in bundle: {m.name!r}")
            tf.extractall(tmp, filter="data")
        names = {m.name for m in members}
        for name in staged:
            if name not in names:
                raise BackupError(f"verification failed: {name} missing "
                                  "from bundle")
        for name in staged:
            path = os.path.join(tmp, name)
            if name.endswith(".db"):
                conn = sqlite3.connect(path, timeout=10)
                try:
                    row = conn.execute("PRAGMA quick_check").fetchone()
                finally:
                    conn.close()
                if not row or row[0] != "ok":
                    raise BackupError(f"verification failed: {name} "
                                      f"integrity check: {row}")
            elif name.endswith(".json"):
                with open(path, "r", encoding="utf-8") as f:
                    json.load(f)
    finally:
        # Best-effort cleanup of the temp dir (contains decrypted data —
        # never leave it behind on success or failure).
        for root, dirs, files in os.walk(tmp, topdown=False):
            for fn in files:
                try:
                    os.unlink(os.path.join(root, fn))
                except OSError:
                    pass
            for dn in dirs:
                try:
                    os.rmdir(os.path.join(root, dn))
                except OSError:
                    pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass


def _atomic_write_bytes(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _prune(backup_dir, keep=KEEP):
    """Keep the newest `keep` ron_backup_*.bin files, delete older ones.

    Also sweeps stale *.tmp files from crashed atomic writes — they are
    never valid snapshots.
    """
    files = sorted(f for f in os.listdir(backup_dir)
                   if f.startswith("ron_backup_") and f.endswith(".bin"))
    for fn in os.listdir(backup_dir):
        if fn.startswith("ron_backup_") and fn.endswith(".bin.tmp"):
            try:
                os.unlink(os.path.join(backup_dir, fn))
                _log(f"swept stale tmp file: {fn}")
            except OSError:
                pass
    doomed = files[:-keep] if len(files) > keep else []
    for fn in doomed:
        try:
            os.unlink(os.path.join(backup_dir, fn))
            _log(f"pruned old snapshot: {fn}")
        except OSError as e:
            _log(f"prune failed for {fn}: {e}")
    return len(files) - len(doomed)


def _write_status(status_path, ok, info, error=None):
    payload = {
        "last_good_ts": info.get("ts") if ok else None,
        "last_file": info.get("file") if ok else None,
        "kept": info.get("kept", 0),
        "last_error": error,
        "last_error_ts": time.time() if error else None,
    }
    # Preserve the previous good-snapshot info on failure: /health should
    # still report the last GOOD backup, not wipe it because one run died.
    if not ok and os.path.exists(status_path):
        try:
            with open(status_path, "r", encoding="utf-8") as f:
                prev = json.load(f)
            for k in ("last_good_ts", "last_file", "kept"):
                if payload[k] in (None, 0) and prev.get(k):
                    payload[k] = prev[k]
        except Exception:
            pass
    tmp = status_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, status_path)


def run_backup(base_dir=BASE_DIR, backup_dir=BACKUP_DIR,
               status_path=STATUS_FILE, key=None, now=None, keep=KEEP):
    """Run the full backup pipeline. Returns True on a verified snapshot.

    All operational failures are caught: they are logged, recorded in the
    status file, and return False. The daemon is never touched.
    """
    now = now if now is not None else time.time()
    try:
        key_raw = _read_key(key)
        fernet = _fernet(key_raw)
        os.makedirs(backup_dir, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ron_backup_stage_") as stage:
            staged = _stage(base_dir, stage)
            bundle = _bundle(stage, staged)
            token = fernet.encrypt(bundle)
            fname = ("ron_backup_%s.bin"
                     % time.strftime("%Y%m%dT%H%M%S", time.gmtime(now)))
            out_path = os.path.join(backup_dir, fname)
            _atomic_write_bytes(out_path, token)
            # Verify what we wrote by reading it back from disk.
            with open(out_path, "rb") as f:
                _verify(f.read(), fernet, staged)
            kept = _prune(backup_dir, keep)
        _write_status(status_path, True,
                      {"ts": now, "file": fname, "kept": kept})
        _log(f"snapshot ok: {fname} ({len(staged)} files, {kept} kept)")
        return True
    except Exception as e:
        # Never include key material: BackupError messages are pre-vetted,
        # and anything else gets truncated with no variable interpolation.
        msg = str(e) if isinstance(e, BackupError) else f"{type(e).__name__}"
        _log(f"FAILED: {msg[:200]}")
        try:
            _write_status(status_path, False, {}, error=msg[:200])
        except Exception as se:
            _log(f"status write failed: {se}")
        return False


def main(argv):
    ok = run_backup()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
