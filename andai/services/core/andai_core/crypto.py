"""Envelope encryption for secrets andai-core stores (platform API keys, TOTP secrets).

Each `seal` draws a fresh 256-bit data key (DEK), encrypts the plaintext with it (AES-256-GCM, the
caller's associated data bound in), and wraps the DEK with the key-encryption key (KEK, also
AES-256-GCM). The stored value is the wrapped DEK and the ciphertext in one base64url string, plus
the `key_id` of the KEK that wrapped it, so a later key rotation can tell which KEK opens a row.

The KEK is 32 bytes, given base64-encoded in `ANDAI_KEK` or in the file named by `ANDAI_KEK_FILE`.
Outside `ANDAI_ENV=dev` a missing KEK is refused; in dev an ephemeral random KEK is used, so
anything sealed in dev is unreadable after a restart.

Blob layout (before base64url): version (1) | KEK nonce (12) | wrapped DEK (48) | DEK nonce (12) |
ciphertext + tag.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from andai_core.settings import Settings

log = logging.getLogger(__name__)

KEY_BYTES = 32
NONCE_BYTES = 12
_VERSION = b"\x01"
_WRAPPED_DEK_BYTES = KEY_BYTES + 16  # AES-GCM appends a 16-byte tag
_HEADER_BYTES = 1 + NONCE_BYTES + _WRAPPED_DEK_BYTES + NONCE_BYTES
_WRAP_AAD = b"andai/dek/v1"
_DATA_AAD = b"andai/data/v1\x00"


class CryptoError(Exception):
    """Base class; messages never contain key material or plaintext."""


class KeyUnavailable(CryptoError):
    """No usable KEK: missing outside dev, malformed, or ambiguous."""


class DecryptionError(CryptoError):
    """The value cannot be opened: wrong key, wrong associated data, or tampered."""


@dataclass(frozen=True)
class Sealed:
    ciphertext: str
    key_id: str


def key_id_for(kek: bytes) -> str:
    """A stable, non-secret identifier for a KEK (a truncated hash, not the key)."""
    return "kek-" + hashlib.sha256(b"andai/kek-id\x00" + kek).hexdigest()[:16]


def _as_bytes(value: bytes | str) -> bytes:
    return value.encode() if isinstance(value, str) else bytes(value)


class Keyring:
    """Seals and opens with one KEK, named by `key_id`; values carry the key_id that sealed them."""

    def __init__(self, kek: bytes, *, ephemeral: bool = False) -> None:
        if len(kek) != KEY_BYTES:
            raise KeyUnavailable(f"KEK must be {KEY_BYTES} bytes")
        self.key_id = key_id_for(kek)
        self.ephemeral = ephemeral
        self._keks = {self.key_id: AESGCM(kek)}

    def seal(self, plaintext: bytes | str, aad: bytes | str) -> Sealed:
        dek = AESGCM.generate_key(bit_length=KEY_BYTES * 8)
        kek_nonce, dek_nonce = os.urandom(NONCE_BYTES), os.urandom(NONCE_BYTES)
        wrapped = self._keks[self.key_id].encrypt(kek_nonce, dek, _WRAP_AAD)
        body = AESGCM(dek).encrypt(dek_nonce, _as_bytes(plaintext), _DATA_AAD + _as_bytes(aad))
        blob = _VERSION + kek_nonce + wrapped + dek_nonce + body
        return Sealed(base64.urlsafe_b64encode(blob).decode("ascii"), self.key_id)

    def open(self, ciphertext: str, key_id: str, aad: bytes | str) -> bytes:
        kek = self._keks.get(key_id)
        if kek is None:
            raise DecryptionError("unknown key id")
        try:
            blob = base64.urlsafe_b64decode(ciphertext.encode("ascii"))
        except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
            raise DecryptionError("malformed ciphertext") from exc
        if len(blob) < _HEADER_BYTES + 16 or blob[:1] != _VERSION:
            raise DecryptionError("malformed ciphertext")
        kek_nonce = blob[1 : 1 + NONCE_BYTES]
        wrapped = blob[1 + NONCE_BYTES : 1 + NONCE_BYTES + _WRAPPED_DEK_BYTES]
        dek_nonce = blob[_HEADER_BYTES - NONCE_BYTES : _HEADER_BYTES]
        body = blob[_HEADER_BYTES:]
        try:
            dek = kek.decrypt(kek_nonce, wrapped, _WRAP_AAD)
            return AESGCM(dek).decrypt(dek_nonce, body, _DATA_AAD + _as_bytes(aad))
        except InvalidTag as exc:
            raise DecryptionError("cannot open sealed value") from exc


def _decode_kek(encoded: str, source: str) -> bytes:
    try:
        kek = base64.b64decode(encoded.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KeyUnavailable(f"{source} is not valid base64") from exc
    if len(kek) != KEY_BYTES:
        raise KeyUnavailable(f"{source} must decode to {KEY_BYTES} bytes")
    return kek


def load_keyring(settings: Settings) -> Keyring:
    """The keyring from `ANDAI_KEK` / `ANDAI_KEK_FILE`; refuses a missing KEK outside dev."""
    inline = settings.kek.get_secret_value() if settings.kek is not None else ""
    path = settings.kek_file
    if inline.strip() and path is not None:
        raise KeyUnavailable("set ANDAI_KEK or ANDAI_KEK_FILE, not both")
    if path is not None:
        try:
            encoded = path.read_text()
        except OSError as exc:
            raise KeyUnavailable("ANDAI_KEK_FILE cannot be read") from exc
        return Keyring(_decode_kek(encoded, "ANDAI_KEK_FILE"))
    if inline.strip():
        return Keyring(_decode_kek(inline, "ANDAI_KEK"))
    if not settings.is_dev:
        raise KeyUnavailable("ANDAI_KEK or ANDAI_KEK_FILE is required when ANDAI_ENV is not dev")
    log.warning(
        "ANDAI_KEK is not set: using an ephemeral key (dev only; sealed values do not survive"
        " a restart)"
    )
    return Keyring(os.urandom(KEY_BYTES), ephemeral=True)
