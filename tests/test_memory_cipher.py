#!/usr/bin/env python3
"""Cipher unit tests for ron_memory's crypto layer.

StdCipher construction (roundtrip, tamper, wrong-key, magic, nonce
uniqueness), FernetCipher behavior when `cryptography` is missing,
make_cipher() selection incl. fail-closed, and key derivation. No
network, no Telegram. DB files only via temp paths.
"""
import base64
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import ron_memory
from ron_memory import (CipherError, FernetCipher, MemoryStore,
                        MemoryStoreError, StdCipher, generate_key,
                        has_cryptography, make_cipher)

KEY = generate_key()
KEY2 = generate_key()
MATERIAL = base64.urlsafe_b64decode(KEY)
MATERIAL2 = base64.urlsafe_b64decode(KEY2)


class StdCipherTest(unittest.TestCase):
    def setUp(self):
        self.c = StdCipher(MATERIAL)

    def test_roundtrip(self):
        for pt in (b"", b"a", "héllo wörld ✓".encode("utf-8"),
                   os.urandom(100_000)):
            self.assertEqual(self.c.decrypt(self.c.encrypt(pt)), pt)

    def test_blob_layout(self):
        blob = self.c.encrypt(b"hello")
        self.assertTrue(blob.startswith(b"RM1"))
        self.assertEqual(len(blob), 3 + 16 + 5 + 16)

    def test_nonce_uniqueness(self):
        b1 = self.c.encrypt(b"same plaintext")
        b2 = self.c.encrypt(b"same plaintext")
        self.assertNotEqual(b1, b2)  # random nonce -> no two blobs alike
        self.assertEqual(self.c.decrypt(b1), b"same plaintext")
        self.assertEqual(self.c.decrypt(b2), b"same plaintext")

    def test_wrong_key_fails(self):
        blob = self.c.encrypt(b"secret")
        with self.assertRaises(CipherError):
            StdCipher(MATERIAL2).decrypt(blob)

    def test_tampered_ciphertext_fails(self):
        blob = bytearray(self.c.encrypt(b"secret"))
        blob[20] ^= 0x01
        with self.assertRaises(CipherError):
            self.c.decrypt(bytes(blob))

    def test_tampered_tag_fails(self):
        blob = bytearray(self.c.encrypt(b"secret"))
        blob[-1] ^= 0x01
        with self.assertRaises(CipherError):
            self.c.decrypt(bytes(blob))

    def test_tampered_nonce_fails(self):
        blob = bytearray(self.c.encrypt(b"secret"))
        blob[5] ^= 0x01
        with self.assertRaises(CipherError):
            self.c.decrypt(bytes(blob))

    def test_bad_magic_fails(self):
        with self.assertRaises(CipherError):
            self.c.decrypt(b"XXX" + b"\x00" * 40)

    def test_short_blob_fails(self):
        with self.assertRaises(CipherError):
            self.c.decrypt(b"RM1short")

    def test_bad_material_rejected(self):
        with self.assertRaises(CipherError):
            StdCipher(b"too short")


class FernetCipherTest(unittest.TestCase):
    @unittest.skipIf(has_cryptography(), "cryptography is installed")
    def test_missing_package_raises(self):
        with self.assertRaises(MemoryStoreError):
            FernetCipher(KEY.encode())

    @unittest.skipUnless(has_cryptography(), "needs cryptography")
    def test_fernet_roundtrip(self):
        c = FernetCipher(KEY.encode())
        self.assertEqual(c.decrypt(c.encrypt(b"hello")), b"hello")


class MakeCipherTest(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get(ron_memory.KEY_ENV)
        self._saved_pref = os.environ.get(ron_memory.CIPHER_ENV)
        os.environ[ron_memory.KEY_ENV] = KEY
        os.environ.pop(ron_memory.CIPHER_ENV, None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(ron_memory.KEY_ENV, None)
        else:
            os.environ[ron_memory.KEY_ENV] = self._saved
        if self._saved_pref is None:
            os.environ.pop(ron_memory.CIPHER_ENV, None)
        else:
            os.environ[ron_memory.CIPHER_ENV] = self._saved_pref

    @unittest.skipIf(has_cryptography(), "cryptography is installed")
    def test_auto_falls_back_to_stdlib(self):
        self.assertIsInstance(make_cipher(), StdCipher)

    @unittest.skipUnless(has_cryptography(), "needs cryptography")
    def test_auto_prefers_fernet(self):
        self.assertIsInstance(make_cipher(), FernetCipher)

    @unittest.skipIf(has_cryptography(), "cryptography is installed")
    def test_prefer_fernet_raises_without_package(self):
        with self.assertRaises(MemoryStoreError):
            make_cipher(prefer="fernet")

    def test_prefer_stdlib(self):
        self.assertIsInstance(make_cipher(prefer="stdlib"), StdCipher)

    def test_env_prefer(self):
        os.environ[ron_memory.CIPHER_ENV] = "stdlib"
        self.assertIsInstance(make_cipher(), StdCipher)

    def test_bad_prefer_raises(self):
        with self.assertRaises(MemoryStoreError):
            make_cipher(prefer="rot13")

    def test_fail_closed_without_key(self):
        os.environ.pop(ron_memory.KEY_ENV, None)
        with self.assertRaises(MemoryStoreError):
            make_cipher()


class KeyDerivationTest(unittest.TestCase):
    def test_generate_key_format(self):
        k = generate_key()
        self.assertEqual(len(base64.urlsafe_b64decode(k.encode())), 32)
        self.assertNotEqual(generate_key(), generate_key())

    def test_fernet_key_passthrough(self):
        # Contract frozen for ron_backup._fernet: output feeds Fernet().
        self.assertEqual(ron_memory._derive_key(KEY), KEY.encode())

    def test_passphrase_derivation(self):
        d1 = ron_memory._derive_key("a human passphrase")
        d2 = ron_memory._derive_key("a human passphrase")
        self.assertEqual(d1, d2)
        self.assertNotEqual(d1, b"a human passphrase")
        self.assertEqual(len(base64.urlsafe_b64decode(d1)), 32)
        self.assertNotEqual(ron_memory._derive_key("different words"), d1)

    def test_missing_key_raises(self):
        with self.assertRaises(MemoryStoreError):
            ron_memory._derive_key("")
        with self.assertRaises(MemoryStoreError):
            ron_memory._derive_key(None)


class StoreCipherIntegrationTest(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get(ron_memory.KEY_ENV)
        os.environ.pop(ron_memory.KEY_ENV, None)
        self.paths = []

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(ron_memory.KEY_ENV, None)
        else:
            os.environ[ron_memory.KEY_ENV] = self._saved
        for p in self.paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    def _tmp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.paths.append(tmp.name)
        return tmp.name

    def test_injected_cipher_needs_no_env_key(self):
        s = MemoryStore(path=self._tmp(), cipher=StdCipher(MATERIAL))
        try:
            self.assertEqual(s.cipher_kind, "stdlib")
            s.remember("A", "injected cipher works")
            self.assertIn("injected",
                          s.recall_facts("A", "injected")[0]["text"])
        finally:
            s.close()

    def test_wrong_key_store_cannot_read(self):
        p = self._tmp()
        s1 = MemoryStore(path=p, cipher=StdCipher(MATERIAL))
        secret = "cross-key secret seven"
        s1.remember("A", "fact holding " + secret)
        s1.close()
        s2 = MemoryStore(path=p, cipher=StdCipher(MATERIAL2))
        try:
            with self.assertRaises(MemoryStoreError) as cm:
                s2.recall_facts("A", "secret seven")
            # the error must not leak the plaintext or key material
            self.assertNotIn(secret, str(cm.exception))
        finally:
            s2.close()

    def test_cipher_mismatch_is_detected(self):
        # A blob written by StdCipher is rejected as foreign by a store
        # on the other cipher: magic check fails fast, no silent garbage.
        p = self._tmp()
        s = MemoryStore(path=self._tmp(), cipher=StdCipher(MATERIAL))
        try:
            blob = s._enc("hello")
            self.assertTrue(blob.startswith(b"RM1"))
            with self.assertRaises(CipherError):
                # Fernet tokens never start with the RM1 magic
                StdCipher(MATERIAL).decrypt(b"gAAAAAB" + b"x" * 40)
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
