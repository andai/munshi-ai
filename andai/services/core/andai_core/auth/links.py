"""Addresses and emailed links.

Links point at portal pages (`/reset-password`, `/invite`) on `ANDAI_PORTAL_ORIGIN`; the portal
posts the token back to andai-core. Tokens are base64url, so they need no escaping.
"""

from __future__ import annotations

from email_validator import EmailNotValidError, validate_email

from andai_core.auth.csrf import normalize_origin
from andai_core.settings import Settings


class InvalidEmail(ValueError):
    """Not a usable email address."""


def canonical_email(raw: str) -> str:
    """The stored form of an address: syntax-checked (no DNS lookup) and lowercased. Reserved
    names (`.invalid`, `.local`, `localhost`, ...) are refused; `.test` is allowed for test
    and development mail."""
    try:
        checked = validate_email(raw.strip(), check_deliverability=False, test_environment=True)
    except EmailNotValidError as exc:
        raise InvalidEmail("not a valid email address") from exc
    return checked.normalized.lower()


def portal_link(settings: Settings, page: str, token: str) -> str:
    return f"{normalize_origin(settings.portal_origin)}/{page}?token={token}"
