"""Opaque bearer tokens (session cookies, reset and invite links).

A token is 32 random bytes, base64url-encoded; only its SHA-256 (hex) is ever stored, so a
database read never yields a usable token.
"""

from __future__ import annotations

import hashlib
import secrets

TOKEN_BYTES = 32


def new_token() -> tuple[str, str]:
    """A fresh token and the hash to store for it."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
