"""TOTP two-factor login (RFC 6238: SHA-1, 6 digits, 30-second steps, one step of clock skew).

Enrollment is two calls. `start` draws a new secret, seals it with the keyring (envelope
encryption, the user id bound in as associated data) and returns the `otpauth://` URI and its QR
code once; nothing returns the secret afterwards. `confirm` accepts the first valid code, marks the
enrollment confirmed and returns 10 recovery codes once; only their argon2id hashes are stored.

`check` verifies a code for a confirmed enrollment: six digits are a TOTP code, anything else a
recovery code. A TOTP code is accepted only for a time step later than the last accepted one
(`user_totp.last_used_step`), so a code cannot be replayed; a recovery code is marked used in the
same statement that claims it, so it works once.

`verify_pending` is the second login step: it locks the `mfa_pending` session, checks the code and
either replaces the session with a new `full` one (a new token) or counts the failure. The fifth
wrong code ends the session; the user signs in again. Wrong codes are also counted per user
across sessions (`MFA_FAILURE_LIMIT` per `MFA_FAILURE_WINDOW_SECONDS`), so signing in again does
not buy unlimited guesses.

`user_totp` and `recovery_codes` are global tables filtered by user id; audit events go to the
org of the session that caused them.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime

import pyotp
import segno
from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session as Tx

from andai_core import audit
from andai_core.auth import passwords, sessions
from andai_core.auth.ratelimit import Limiter
from andai_core.auth.tokens import hash_token
from andai_core.crypto import Keyring
from andai_core.db.models import RecoveryCode, Session, User, UserTotp
from andai_core.db.session import set_context

ISSUER = "munshi.ai"
SKEW_STEPS = 1
MAX_SESSION_FAILURES = 5
MFA_FAILURE_LIMIT = 10
MFA_FAILURE_WINDOW_SECONDS = 15 * 60
RECOVERY_CODE_COUNT = 10
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
_RECOVERY_HALF = 5
_TOTP_CODE = re.compile(r"\d{6}")

TOTP = "totp"
RECOVERY = "recovery"


class MfaError(Exception):
    """Base class; messages never contain a secret or a code."""


class NotEnrolled(MfaError):
    """The user has no confirmed two-factor enrollment (or, for `confirm`, none to confirm)."""


class AlreadyEnrolled(MfaError):
    """The user already has a confirmed enrollment."""


class InvalidCode(MfaError):
    """The code is wrong, replayed or already used."""


@dataclass(frozen=True)
class Enrollment:
    otpauth_uri: str
    qr_svg: str


def failure_key(user_id: uuid.UUID) -> str:
    return "mfa:" + hashlib.sha256(f"mfa\x00{user_id}".encode()).hexdigest()


def _aad(user_id: uuid.UUID) -> str:
    return f"user_totp:{user_id}"


def _normalize(code: str) -> str:
    return re.sub(r"[\s-]", "", code).lower()


def qr_svg(uri: str) -> str:
    buffer = io.BytesIO()
    segno.make(uri, error="m").save(buffer, kind="svg", scale=4, xmldecl=False)
    return buffer.getvalue().decode("utf-8")


def is_enrolled(tx: Tx, user_id: uuid.UUID) -> bool:
    return (
        tx.scalar(
            select(UserTotp.user_id).where(
                UserTotp.user_id == user_id, UserTotp.confirmed_at.is_not(None)
            )
        )
        is not None
    )


def start(tx: Tx, keyring: Keyring, *, user_id: uuid.UUID, email: str, now: datetime) -> Enrollment:
    """Begin (or restart) enrollment with a fresh secret; refused once one is confirmed."""
    row = tx.get(UserTotp, user_id, with_for_update=True)
    if row is not None and row.confirmed_at is not None:
        raise AlreadyEnrolled
    secret = pyotp.random_base32()
    sealed = keyring.seal(secret, _aad(user_id))
    if row is None:
        tx.add(
            UserTotp(
                user_id=user_id,
                secret_ciphertext=sealed.ciphertext,
                key_id=sealed.key_id,
                created_at=now,
            )
        )
    else:
        row.secret_ciphertext = sealed.ciphertext
        row.key_id = sealed.key_id
        row.last_used_step = None
        row.created_at = now
    tx.flush()
    uri = pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)
    return Enrollment(otpauth_uri=uri, qr_svg=qr_svg(uri))


def _totp(keyring: Keyring, row: UserTotp) -> pyotp.TOTP:
    secret = keyring.open(row.secret_ciphertext, row.key_id, _aad(row.user_id)).decode("ascii")
    return pyotp.TOTP(secret)


def _matching_step(totp: pyotp.TOTP, code: str, now: datetime, last_used: int | None) -> int | None:
    """The time step (within the allowed skew, after `last_used`) whose code is `code`."""
    current = totp.timecode(now)
    for step in range(current - SKEW_STEPS, current + SKEW_STEPS + 1):
        if last_used is not None and step <= last_used:
            continue
        if hmac.compare_digest(totp.generate_otp(step), code):
            return step
    return None


def _claim_step(tx: Tx, user_id: uuid.UUID, step: int) -> bool:
    result = tx.execute(
        update(UserTotp)
        .where(
            UserTotp.user_id == user_id,
            or_(UserTotp.last_used_step.is_(None), UserTotp.last_used_step < step),
        )
        .values(last_used_step=step)
    )
    return result.rowcount == 1


def _new_recovery_code() -> str:
    chars = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(2 * _RECOVERY_HALF))
    return f"{chars[:_RECOVERY_HALF]}-{chars[_RECOVERY_HALF:]}"


def _replace_recovery_codes(tx: Tx, user_id: uuid.UUID, now: datetime) -> list[str]:
    tx.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user_id))
    codes = [_new_recovery_code() for _ in range(RECOVERY_CODE_COUNT)]
    for code in codes:
        tx.add(
            RecoveryCode(
                id=uuid.uuid4(),
                user_id=user_id,
                code_hash=passwords.hash_password(_normalize(code)),
                created_at=now,
            )
        )
    tx.flush()
    return codes


def _use_recovery_code(tx: Tx, user_id: uuid.UUID, code: str, now: datetime) -> bool:
    if len(code) != 2 * _RECOVERY_HALF or any(c not in _RECOVERY_ALPHABET for c in code):
        return False
    unused = tx.execute(
        select(RecoveryCode.id, RecoveryCode.code_hash).where(
            RecoveryCode.user_id == user_id, RecoveryCode.used_at.is_(None)
        )
    ).all()
    for code_id, code_hash in unused:
        if passwords.verify(code_hash, code):
            claimed = tx.execute(
                update(RecoveryCode)
                .where(RecoveryCode.id == code_id, RecoveryCode.used_at.is_(None))
                .values(used_at=now)
            )
            return claimed.rowcount == 1
    return False


def confirm(
    tx: Tx, keyring: Keyring, *, user_id: uuid.UUID, code: str, now: datetime
) -> list[str]:
    """Confirm a started enrollment with a valid TOTP code; returns the new recovery codes."""
    row = tx.get(UserTotp, user_id, with_for_update=True)
    if row is None or row.confirmed_at is not None:
        raise NotEnrolled
    code = _normalize(code)
    if not _TOTP_CODE.fullmatch(code):
        raise InvalidCode
    step = _matching_step(_totp(keyring, row), code, now, row.last_used_step)
    if step is None:
        raise InvalidCode
    row.last_used_step = step
    row.confirmed_at = now
    return _replace_recovery_codes(tx, user_id, now)


def check(tx: Tx, keyring: Keyring, *, user_id: uuid.UUID, code: str, now: datetime) -> str:
    """Accept a TOTP or recovery code for a confirmed enrollment; returns `TOTP` or `RECOVERY`.

    Raises `NotEnrolled` without a confirmed enrollment and `InvalidCode` for a wrong, replayed or
    used code. The accepted code is consumed in `tx`."""
    row = tx.get(UserTotp, user_id, with_for_update=True)
    if row is None or row.confirmed_at is None:
        raise NotEnrolled
    code = _normalize(code)
    if _TOTP_CODE.fullmatch(code):
        step = _matching_step(_totp(keyring, row), code, now, row.last_used_step)
        if step is not None and _claim_step(tx, user_id, step):
            return TOTP
        raise InvalidCode
    if _use_recovery_code(tx, user_id, code, now):
        return RECOVERY
    raise InvalidCode


def regenerate_recovery_codes(tx: Tx, *, user_id: uuid.UUID, now: datetime) -> list[str]:
    if not is_enrolled(tx, user_id):
        raise NotEnrolled
    return _replace_recovery_codes(tx, user_id, now)


def remove(tx: Tx, user_id: uuid.UUID) -> bool:
    """Delete the user's TOTP enrollment and recovery codes; True if there was an enrollment."""
    tx.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user_id))
    return tx.execute(delete(UserTotp).where(UserTotp.user_id == user_id)).rowcount > 0


def record(
    tx: Tx,
    *,
    org_id: uuid.UUID,
    actor: uuid.UUID | None,
    action: str,
    user_id: uuid.UUID,
    ip: str | None,
) -> None:
    """One audit event about `user_id`'s two-factor login, in `org_id`."""
    set_context(tx, org_id=org_id)
    audit.record(tx, org_id, actor, action, f"user:{user_id}", ip)


# ─── the second login step ────────────────────────────────────────────────────────────────────────

VERIFIED = "verified"
WRONG = "wrong"
LOCKED = "locked"
THROTTLED = "throttled"
NO_PENDING = "no_pending"


@dataclass(frozen=True)
class VerifyOutcome:
    status: str
    token: str | None = None
    retry_after: int | None = None


def verify_pending(
    tx: Tx,
    keyring: Keyring,
    limiter: Limiter,
    *,
    token: str,
    code: str,
    lifetimes: sessions.Lifetimes,
    now: datetime,
    ip: str | None,
    user_agent: str | None,
) -> VerifyOutcome:
    """Complete an `mfa_pending` login with a code. The session row stays locked for the whole
    check, so concurrent guesses on one session are counted one by one."""
    row = tx.execute(
        select(Session, User)
        .join(User, User.id == Session.user_id)
        .where(Session.token_hash == hash_token(token))
        .with_for_update(of=Session)
    ).first()
    if row is None:
        return VerifyOutcome(NO_PENDING)
    pending, user = row
    if pending.stage != sessions.MFA_PENDING:
        return VerifyOutcome(NO_PENDING)
    if now >= pending.expires_at or user.status != "active":
        sessions.revoke_id(tx, pending.id)
        return VerifyOutcome(NO_PENDING)

    key = failure_key(user.id)
    retry_after = limiter.blocked(key)
    if retry_after is not None:
        return VerifyOutcome(THROTTLED, retry_after=retry_after)

    try:
        method = check(tx, keyring, user_id=user.id, code=code, now=now)
    except NotEnrolled:
        # The enrollment was removed (admin reset) after the password step: start over.
        sessions.revoke_id(tx, pending.id)
        return VerifyOutcome(NO_PENDING)
    except InvalidCode:
        limiter.hit(key)
        pending.mfa_failures += 1
        if pending.mfa_failures >= MAX_SESSION_FAILURES:
            sessions.revoke_id(tx, pending.id)
            record(
                tx,
                org_id=pending.org_id,
                actor=user.id,
                action="mfa.lockout",
                user_id=user.id,
                ip=ip,
            )
            return VerifyOutcome(LOCKED)
        return VerifyOutcome(WRONG)

    org_id = pending.org_id
    sessions.revoke_id(tx, pending.id)
    new = sessions.create(
        tx,
        user_id=user.id,
        org_id=org_id,
        stage=sessions.FULL,
        lifetimes=lifetimes,
        now=now,
        ip=ip,
        user_agent=user_agent,
    )
    if method == RECOVERY:
        record(
            tx, org_id=org_id, actor=user.id, action="mfa.recovery_use", user_id=user.id, ip=ip
        )
    limiter.clear(key)
    return VerifyOutcome(VERIFIED, token=new)
