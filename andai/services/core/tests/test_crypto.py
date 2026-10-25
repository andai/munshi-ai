"""Envelope encryption: round trip, associated data, tamper detection, KEK loading."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

from andai_core.crypto import (
    DecryptionError,
    Keyring,
    KeyUnavailable,
    Sealed,
    key_id_for,
    load_keyring,
)
from andai_core.settings import Settings

AAD = b"platform_principals:org-1"


def _kek() -> bytes:
    return os.urandom(32)


@pytest.fixture
def settings_from(monkeypatch: pytest.MonkeyPatch):
    """Settings built from exactly the given KEK-related environment variables."""

    def build(**env: str) -> Settings:
        for key in ("ANDAI_ENV", "ANDAI_KEK", "ANDAI_KEK_FILE"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return Settings()

    return build


def test_round_trip() -> None:
    keyring = Keyring(_kek())
    sealed = keyring.seal(b"platform-api-key", AAD)
    assert isinstance(sealed, Sealed)
    assert sealed.key_id == keyring.key_id
    assert b"platform-api-key" not in base64.urlsafe_b64decode(sealed.ciphertext)
    assert keyring.open(sealed.ciphertext, sealed.key_id, AAD) == b"platform-api-key"


def test_str_plaintext_and_aad() -> None:
    keyring = Keyring(_kek())
    sealed = keyring.seal("JBSWY3DPEHPK3PXP", "user_totp:u-1")
    assert keyring.open(sealed.ciphertext, sealed.key_id, "user_totp:u-1") == b"JBSWY3DPEHPK3PXP"


def test_each_seal_uses_fresh_randomness() -> None:
    keyring = Keyring(_kek())
    first, second = keyring.seal(b"same", AAD), keyring.seal(b"same", AAD)
    assert first.ciphertext != second.ciphertext
    assert first.key_id == second.key_id


def test_wrong_aad_fails() -> None:
    keyring = Keyring(_kek())
    sealed = keyring.seal(b"secret", AAD)
    with pytest.raises(DecryptionError):
        keyring.open(sealed.ciphertext, sealed.key_id, b"platform_principals:org-2")


@pytest.mark.parametrize(
    "offset",
    [0, 1, 13, 40, 61, 73, -17, -1],
    ids=[
        "version", "kek-nonce", "wrapped-dek", "wrapped-dek-tag", "dek-nonce", "body", "body-end",
        "tag",
    ],
)
def test_tampered_ciphertext_fails(offset: int) -> None:
    keyring = Keyring(_kek())
    sealed = keyring.seal(b"secret value", AAD)
    blob = bytearray(base64.urlsafe_b64decode(sealed.ciphertext))
    blob[offset] ^= 0x01
    tampered = base64.urlsafe_b64encode(bytes(blob)).decode()
    with pytest.raises(DecryptionError):
        keyring.open(tampered, sealed.key_id, AAD)


@pytest.mark.parametrize(
    "ciphertext", ["", "not base64!", base64.urlsafe_b64encode(b"\x01short").decode()]
)
def test_malformed_ciphertext_fails(ciphertext: str) -> None:
    keyring = Keyring(_kek())
    with pytest.raises(DecryptionError):
        keyring.open(ciphertext, keyring.key_id, AAD)


def test_other_kek_cannot_open() -> None:
    sealed = Keyring(_kek()).seal(b"secret", AAD)
    other = Keyring(_kek())
    with pytest.raises(DecryptionError):
        other.open(sealed.ciphertext, sealed.key_id, AAD)  # unknown key id
    with pytest.raises(DecryptionError):
        other.open(sealed.ciphertext, other.key_id, AAD)  # right id claimed, wrong key


def test_key_id_is_stable_and_not_the_key() -> None:
    kek = _kek()
    assert key_id_for(kek) == Keyring(kek).key_id
    assert key_id_for(kek) != key_id_for(_kek())
    assert kek.hex() not in key_id_for(kek)


def test_missing_kek_refused_outside_dev(settings_from) -> None:
    with pytest.raises(KeyUnavailable):
        load_keyring(settings_from(ANDAI_ENV="production"))


def test_missing_kek_in_dev_uses_ephemeral_key(settings_from) -> None:
    keyring = load_keyring(settings_from(ANDAI_ENV="dev"))
    assert keyring.ephemeral
    sealed = keyring.seal(b"x", AAD)
    assert keyring.open(sealed.ciphertext, sealed.key_id, AAD) == b"x"


def test_kek_from_env(settings_from) -> None:
    kek = _kek()
    encoded = base64.b64encode(kek).decode()
    keyring = load_keyring(settings_from(ANDAI_ENV="production", ANDAI_KEK=encoded))
    assert keyring.key_id == key_id_for(kek) and not keyring.ephemeral


def test_kek_from_file(tmp_path: Path, settings_from) -> None:
    kek = _kek()
    path = tmp_path / "kek"
    path.write_text(base64.b64encode(kek).decode() + "\n")
    path.chmod(0o600)
    keyring = load_keyring(settings_from(ANDAI_ENV="production", ANDAI_KEK_FILE=str(path)))
    assert keyring.key_id == key_id_for(kek)


@pytest.mark.parametrize(
    "value",
    [
        "not-base64!!",
        base64.b64encode(os.urandom(16)).decode(),
        base64.b64encode(os.urandom(33)).decode(),
    ],
    ids=["not-base64", "16-bytes", "33-bytes"],
)
def test_malformed_kek_refused(value: str, settings_from) -> None:
    with pytest.raises(KeyUnavailable):
        load_keyring(settings_from(ANDAI_ENV="dev", ANDAI_KEK=value))


def test_kek_inline_and_file_together_refused(tmp_path: Path, settings_from) -> None:
    path = tmp_path / "kek"
    path.write_text(base64.b64encode(_kek()).decode())
    with pytest.raises(KeyUnavailable):
        load_keyring(
            settings_from(
                ANDAI_ENV="production",
                ANDAI_KEK=base64.b64encode(_kek()).decode(),
                ANDAI_KEK_FILE=str(path),
            )
        )
