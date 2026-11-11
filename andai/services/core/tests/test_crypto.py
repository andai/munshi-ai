"""Envelope encryption: round trip, associated data, tamper detection, KEK loading, and key
rotation (active + retired keys, re-wrap without touching the encrypted body)."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

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
        for key in ("ANDAI_ENV", "ANDAI_KEK", "ANDAI_KEK_FILE", "ANDAI_KEK_RETIRED_FILE"):
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


# ─── key rotation ────────────────────────────────────────────────────────────────────────────────


def _b64(kek: bytes) -> str:
    return base64.b64encode(kek).decode()


def _parts(ciphertext: str) -> tuple[bytes, bytes, bytes]:
    """(KEK nonce, wrapped DEK, DEK nonce + body) of a sealed value."""
    blob = base64.urlsafe_b64decode(ciphertext)
    return blob[1:13], blob[13:61], blob[61:]


def _pre_rotation_seal(kek: bytes, plaintext: bytes, aad: bytes) -> str:
    """The blob exactly as the single-key code wrote it before key rotation existed."""
    dek = AESGCM.generate_key(bit_length=256)
    kek_nonce, dek_nonce = os.urandom(12), os.urandom(12)
    wrapped = AESGCM(kek).encrypt(kek_nonce, dek, b"andai/dek/v1")
    body = AESGCM(dek).encrypt(dek_nonce, plaintext, b"andai/data/v1\x00" + aad)
    return base64.urlsafe_b64encode(b"\x01" + kek_nonce + wrapped + dek_nonce + body).decode()


def test_a_value_sealed_before_rotation_existed_opens_with_a_one_key_keyring() -> None:
    kek = _kek()
    ciphertext = _pre_rotation_seal(kek, b"legacy-secret", AAD)

    keyring = Keyring(kek)

    assert keyring.retired_ids == ()
    assert keyring.open(ciphertext, key_id_for(kek), AAD) == b"legacy-secret"


def test_seals_with_the_active_key_and_opens_with_any_loaded_key() -> None:
    old, new = _kek(), _kek()
    under_old = Keyring(old).seal(b"old-secret", AAD)

    keyring = Keyring(new, [old])
    fresh = keyring.seal(b"new-secret", AAD)

    assert keyring.key_id == key_id_for(new)
    assert keyring.retired_ids == (key_id_for(old),)
    assert fresh.key_id == key_id_for(new)
    assert keyring.open(under_old.ciphertext, under_old.key_id, AAD) == b"old-secret"
    assert keyring.open(fresh.ciphertext, fresh.key_id, AAD) == b"new-secret"


def test_the_same_key_loaded_twice_is_refused() -> None:
    active, old = _kek(), _kek()
    with pytest.raises(KeyUnavailable, match="more than once"):
        Keyring(active, [old, old])
    with pytest.raises(KeyUnavailable, match="also listed as retired"):
        Keyring(active, [old, active])


def test_a_retired_key_of_the_wrong_length_is_refused() -> None:
    with pytest.raises(KeyUnavailable):
        Keyring(_kek(), [os.urandom(16)])


def test_rewrap_moves_the_value_to_the_active_key_and_keeps_the_body() -> None:
    old, new = _kek(), _kek()
    sealed = Keyring(old).seal(b"platform-api-key", AAD)

    keyring = Keyring(new, [old])
    moved = keyring.rewrap(sealed.ciphertext, sealed.key_id)

    assert moved.key_id == keyring.key_id
    old_nonce, old_wrapped, old_rest = _parts(sealed.ciphertext)
    new_nonce, new_wrapped, new_rest = _parts(moved.ciphertext)
    assert new_rest == old_rest  # DEK nonce and encrypted body byte for byte
    assert new_nonce != old_nonce and new_wrapped != old_wrapped
    assert keyring.open(moved.ciphertext, moved.key_id, AAD) == b"platform-api-key"
    assert Keyring(new).open(moved.ciphertext, moved.key_id, AAD) == b"platform-api-key"
    with pytest.raises(DecryptionError):
        keyring.open(moved.ciphertext, moved.key_id, b"platform_principals:org-2")


def test_rewrap_of_a_value_already_under_the_active_key_changes_nothing() -> None:
    keyring = Keyring(_kek(), [_kek()])
    sealed = keyring.seal(b"x", AAD)

    assert keyring.rewrap(sealed.ciphertext, sealed.key_id) == sealed


def test_rewrap_refuses_an_unknown_key_id_and_a_tampered_wrap() -> None:
    old = _kek()
    sealed = Keyring(old).seal(b"x", AAD)
    with pytest.raises(DecryptionError):
        Keyring(_kek()).rewrap(sealed.ciphertext, sealed.key_id)

    blob = bytearray(base64.urlsafe_b64decode(sealed.ciphertext))
    blob[20] ^= 0x01  # inside the wrapped DEK
    tampered = base64.urlsafe_b64encode(bytes(blob)).decode()
    with pytest.raises(DecryptionError):
        Keyring(_kek(), [old]).rewrap(tampered, sealed.key_id)
    with pytest.raises(DecryptionError):
        Keyring(_kek(), [old]).rewrap("not base64!", sealed.key_id)


def test_retired_keys_from_a_file_with_blank_lines_and_comments(
    tmp_path: Path, settings_from
) -> None:
    active, first, second = _kek(), _kek(), _kek()
    path = tmp_path / "kek-retired"
    path.write_text(
        f"# retired 2026-11 (rotation 1)\n\n{_b64(first)}\n   \n  # next\n{_b64(second)}\n"
    )

    keyring = load_keyring(
        settings_from(
            ANDAI_ENV="production", ANDAI_KEK=_b64(active), ANDAI_KEK_RETIRED_FILE=str(path)
        )
    )

    assert keyring.key_id == key_id_for(active)
    assert keyring.retired_ids == (key_id_for(first), key_id_for(second))


def test_a_blank_retired_file_setting_means_none(settings_from) -> None:
    keyring = load_keyring(
        settings_from(ANDAI_ENV="production", ANDAI_KEK=_b64(_kek()), ANDAI_KEK_RETIRED_FILE="")
    )
    assert keyring.retired_ids == ()


def test_a_bad_retired_line_is_refused_by_line_number_without_its_value(
    tmp_path: Path, settings_from
) -> None:
    path = tmp_path / "kek-retired"
    bad = base64.b64encode(os.urandom(16)).decode()
    path.write_text(f"{_b64(_kek())}\n{bad}\n")

    with pytest.raises(KeyUnavailable) as raised:
        load_keyring(
            settings_from(
                ANDAI_ENV="production", ANDAI_KEK=_b64(_kek()), ANDAI_KEK_RETIRED_FILE=str(path)
            )
        )
    assert "line 2" in str(raised.value) and bad not in str(raised.value)


def test_an_unreadable_retired_file_is_refused(tmp_path: Path, settings_from) -> None:
    with pytest.raises(KeyUnavailable, match="cannot be read"):
        load_keyring(
            settings_from(
                ANDAI_ENV="production",
                ANDAI_KEK=_b64(_kek()),
                ANDAI_KEK_RETIRED_FILE=str(tmp_path / "missing"),
            )
        )


def test_the_active_key_listed_as_retired_is_refused_at_load(
    tmp_path: Path, settings_from
) -> None:
    active = _kek()
    path = tmp_path / "kek-retired"
    path.write_text(_b64(active) + "\n")
    with pytest.raises(KeyUnavailable, match="also listed as retired"):
        load_keyring(
            settings_from(
                ANDAI_ENV="production", ANDAI_KEK=_b64(active), ANDAI_KEK_RETIRED_FILE=str(path)
            )
        )


def test_retired_keys_without_an_active_key_are_refused_in_dev(
    tmp_path: Path, settings_from
) -> None:
    path = tmp_path / "kek-retired"
    path.write_text(_b64(_kek()) + "\n")
    with pytest.raises(KeyUnavailable, match="needs an active"):
        load_keyring(settings_from(ANDAI_ENV="dev", ANDAI_KEK_RETIRED_FILE=str(path)))
