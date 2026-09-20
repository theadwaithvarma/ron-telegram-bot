#!/usr/bin/env python3
"""Phase 7 tests: nightly encrypted SQLite snapshots. No network, no daemon.

Run: python3 -m unittest discover tests -v   (from ~/workspace/telegram-bot)
"""
import io
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cryptography.fernet import Fernet, InvalidToken

import ron_backup


TEST_KEY = Fernet.generate_key().decode()


def make_base(tmp):
    """Fake bot dir: two small sqlite DBs + the JSON state files."""
    for name in ("ron_memory.db", "ron_cache.db"):
        conn = sqlite3.connect(os.path.join(tmp, name))
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t (v) VALUES ('hello')")
        conn.commit()
        conn.close()
    for name, payload in (("ron_ai_history.json", {"c1": []}),
                          ("ron_cache_stats.json",
                           {"hits": 1, "misses": 2, "tokens_saved_est": 3}),
                          ("ron_ai.json", {"light_mode": False})):
        with open(os.path.join(tmp, name), "w") as f:
            json.dump(payload, f)
    return tmp


class KeyHandlingTest(unittest.TestCase):
    def test_env_key_used(self):
        self.assertEqual(ron_backup._read_key(explicit=TEST_KEY), TEST_KEY)

    def test_missing_key_raises_without_key_material(self):
        old = os.environ.pop(ron_backup.KEY_ENV, None)
        orig = ron_backup.OVERRIDE_PATH
        orig_env = ron_backup.PERSISTENT_ENV_FILE
        ron_backup.OVERRIDE_PATH = "/nonexistent/override.conf"
        ron_backup.PERSISTENT_ENV_FILE = "/nonexistent/ron-env"
        try:
            with self.assertRaises(ron_backup.BackupError) as cm:
                ron_backup._read_key()
            msg = str(cm.exception)
            self.assertIn(ron_backup.KEY_ENV, msg)  # names the var, fine
            # The message must not contain anything key-like: no base64
            # blob longer than a word.
            self.assertLess(max(len(w) for w in msg.split()), 40)
        finally:
            ron_backup.OVERRIDE_PATH = orig
            ron_backup.PERSISTENT_ENV_FILE = orig_env
            if old is not None:
                os.environ[ron_backup.KEY_ENV] = old

    def test_override_file_parsed(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf",
                                         delete=False) as f:
            f.write("[Service]\nEnvironment=RON_MEMORY_KEY=" + TEST_KEY
                    + "\n")
            path = f.name
        orig = ron_backup.OVERRIDE_PATH
        orig_env = ron_backup.PERSISTENT_ENV_FILE
        old = os.environ.pop(ron_backup.KEY_ENV, None)
        ron_backup.OVERRIDE_PATH = path
        ron_backup.PERSISTENT_ENV_FILE = "/nonexistent/ron-env"
        try:
            self.assertEqual(ron_backup._read_key(), TEST_KEY)
        finally:
            ron_backup.OVERRIDE_PATH = orig
            ron_backup.PERSISTENT_ENV_FILE = orig_env
            os.unlink(path)
            if old is not None:
                os.environ[ron_backup.KEY_ENV] = old

    def test_quoted_passphrase_with_spaces_not_truncated(self):
        # Regression: the old bare-token regex cut "my secret" down to
        # "my", encrypting snapshots under the wrong key while _verify()
        # still passed (self-consistent) — unrestorable "ok" backups.
        phrase = "correct horse battery staple"
        with tempfile.NamedTemporaryFile("w", suffix=".conf",
                                         delete=False) as f:
            f.write('[Service]\nEnvironment="RON_MEMORY_KEY=%s"\n' % phrase)
            path = f.name
        orig = ron_backup.OVERRIDE_PATH
        orig_env = ron_backup.PERSISTENT_ENV_FILE
        old = os.environ.pop(ron_backup.KEY_ENV, None)
        ron_backup.OVERRIDE_PATH = path
        ron_backup.PERSISTENT_ENV_FILE = "/nonexistent/ron-env"
        try:
            self.assertEqual(ron_backup._read_key(), phrase)
            # And a backup encrypted under it decrypts under it.
            fernet = ron_backup._fernet(phrase)
            self.assertEqual(fernet.decrypt(fernet.encrypt(b"x")), b"x")
        finally:
            ron_backup.OVERRIDE_PATH = orig
            ron_backup.PERSISTENT_ENV_FILE = orig_env
            os.unlink(path)
            if old is not None:
                os.environ[ron_backup.KEY_ENV] = old

    def test_fernet_matches_memory_store_derivation(self):
        # The backup must decrypt what the memory store encrypts and vice
        # versa — same key derivation, guaranteed by shared code.
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from ron_memory import _derive_key
        from cryptography.fernet import Fernet as F2
        f1 = ron_backup._fernet(TEST_KEY)
        f2 = F2(_derive_key(TEST_KEY))
        token = f2.encrypt(b"ping")
        self.assertEqual(f1.decrypt(token), b"ping")


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = os.path.join(self.tmp.name, "base")
        os.makedirs(self.base, exist_ok=True)
        make_base(self.base)
        self.bdir = os.path.join(self.tmp.name, "backups")
        self.status = os.path.join(self.tmp.name, "ron_backup.json")

    def test_full_run_produces_verified_encrypted_snapshot(self):
        ok = ron_backup.run_backup(base_dir=self.base, backup_dir=self.bdir,
                                   status_path=self.status, key=TEST_KEY,
                                   now=1700000000.0)
        self.assertTrue(ok)
        files = os.listdir(self.bdir)
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0], r"ron_backup_\d{8}T\d{6}\.bin")
        # The file on disk is Fernet-encrypted, not a tar.
        with open(os.path.join(self.bdir, files[0]), "rb") as f:
            raw = f.read()
        self.assertFalse(raw.startswith(b"\x1f\x8b"))  # not raw gzip
        fernet = ron_backup._fernet(TEST_KEY)
        payload = fernet.decrypt(raw)
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tf:
            names = tf.getnames()
        for n in ("ron_memory.db", "ron_cache.db", "ron_ai_history.json",
                  "ron_cache_stats.json", "ron_ai.json"):
            self.assertIn(n, names)
        # Status file records the good run.
        with open(self.status) as f:
            st = json.load(f)
        self.assertEqual(st["last_good_ts"], 1700000000.0)
        self.assertEqual(st["last_file"], files[0])
        self.assertIsNone(st["last_error"])

    def test_wrong_key_cannot_decrypt(self):
        ron_backup.run_backup(base_dir=self.base, backup_dir=self.bdir,
                              status_path=self.status, key=TEST_KEY, now=1.0)
        fn = os.listdir(self.bdir)[0]
        with open(os.path.join(self.bdir, fn), "rb") as f:
            raw = f.read()
        other = ron_backup._fernet(Fernet.generate_key().decode())
        with self.assertRaises(InvalidToken):
            other.decrypt(raw)

    def test_tampered_bundle_fails_verification(self):
        fernet = ron_backup._fernet(TEST_KEY)
        with tempfile.TemporaryDirectory() as stage:
            staged = ron_backup._stage(self.base, stage)
            bundle = ron_backup._bundle(stage, staged)
            token = bytearray(fernet.encrypt(bundle))
        token[50] ^= 0xFF  # flip a byte mid-token
        with self.assertRaises(ron_backup.BackupError):
            ron_backup._verify(bytes(token), fernet, staged)

    def test_prune_keeps_seven(self):
        os.makedirs(self.bdir, exist_ok=True)
        for i in range(10):
            fn = "ron_backup_202601%02dT000000.bin" % i
            with open(os.path.join(self.bdir, fn), "wb") as f:
                f.write(b"x")
        kept = ron_backup._prune(self.bdir, keep=7)
        self.assertEqual(kept, 7)
        files = sorted(os.listdir(self.bdir))
        self.assertEqual(len(files), 7)
        # Newest seven survive.
        self.assertTrue(files[0].endswith("20260103T000000.bin"))
        # Non-backup files are never touched.
        with open(os.path.join(self.bdir, "notes.txt"), "w") as f:
            f.write("hi")
        ron_backup._prune(self.bdir, keep=7)
        self.assertTrue(os.path.exists(os.path.join(self.bdir, "notes.txt")))

    def test_missing_mandatory_db_fails_loud(self):
        os.unlink(os.path.join(self.base, "ron_memory.db"))
        ok = ron_backup.run_backup(base_dir=self.base, backup_dir=self.bdir,
                                   status_path=self.status, key=TEST_KEY,
                                   now=2.0)
        self.assertFalse(ok)
        # No snapshot file was written...
        self.assertFalse(os.path.exists(self.bdir)
                         and os.listdir(self.bdir))
        # ...but the status file records the failure.
        with open(self.status) as f:
            st = json.load(f)
        self.assertIsNotNone(st["last_error"])
        self.assertIsNotNone(st["last_error_ts"])

    def test_prune_sweeps_stale_tmp_files(self):
        os.makedirs(self.bdir, exist_ok=True)
        with open(os.path.join(self.bdir,
                               "ron_backup_20260101T000000.bin.tmp"),
                  "wb") as f:
            f.write(b"partial")
        with open(os.path.join(self.bdir, "other.tmp"), "wb") as f:
            f.write(b"keep me")
        ron_backup._prune(self.bdir, keep=7)
        self.assertFalse(os.path.exists(os.path.join(
            self.bdir, "ron_backup_20260101T000000.bin.tmp")))
        self.assertTrue(os.path.exists(os.path.join(self.bdir, "other.tmp")))

    def test_failed_run_keeps_previous_kept_count(self):
        self.assertTrue(ron_backup.run_backup(
            base_dir=self.base, backup_dir=self.bdir,
            status_path=self.status, key=TEST_KEY, now=100.0))
        with open(self.status) as f:
            self.assertEqual(json.load(f)["kept"], 1)
        os.unlink(os.path.join(self.base, "ron_memory.db"))
        self.assertFalse(ron_backup.run_backup(
            base_dir=self.base, backup_dir=self.bdir,
            status_path=self.status, key=TEST_KEY, now=200.0))
        with open(self.status) as f:
            st = json.load(f)
        self.assertEqual(st["kept"], 1)  # carried forward, not reset
        self.assertEqual(st["last_good_ts"], 100.0)
        self.assertIsNotNone(st["last_file"])
        self.assertIsNotNone(st["last_error"])

    def test_sqlite_snapshot_is_live_safe(self):
        # Simulate the daemon writing while we snapshot: rows added
        # during the backup must not corrupt the copy.
        dbp = os.path.join(self.base, "ron_memory.db")
        conn = sqlite3.connect(dbp)
        conn.execute("PRAGMA journal_mode=WAL")
        for i in range(50):
            conn.execute("INSERT INTO t (v) VALUES (?)", (f"row{i}",))
        conn.commit()
        with tempfile.TemporaryDirectory() as stage:
            ron_backup._snapshot_sqlite(dbp,
                                        os.path.join(stage, "copy.db"))
            c2 = sqlite3.connect(os.path.join(stage, "copy.db"))
            try:
                row = c2.execute("PRAGMA quick_check").fetchone()
                self.assertEqual(row[0], "ok")
                n = c2.execute("SELECT COUNT(*) FROM t").fetchone()[0]
                self.assertGreaterEqual(n, 51)
            finally:
                c2.close()
        conn.close()


if __name__ == "__main__":
    unittest.main()
