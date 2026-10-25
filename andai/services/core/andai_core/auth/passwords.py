"""Password hashing with argon2id (argon2-cffi's default parameters).

`verify` never raises on a wrong or malformed hash. `dummy_verify` spends the same work on a
throwaway hash, so a login for an unknown email takes as long as one for a known email.
"""

from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 12
MAX_LENGTH = 1024

_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


class WeakPassword(ValueError):
    """The new password does not meet the length rules."""


def check_new(password: str) -> None:
    if len(password) < MIN_LENGTH:
        raise WeakPassword(f"password must be at least {MIN_LENGTH} characters")
    if len(password) > MAX_LENGTH:
        raise WeakPassword(f"password must be at most {MAX_LENGTH} characters")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def dummy_verify(password: str) -> None:
    verify(_DUMMY_HASH, password)
