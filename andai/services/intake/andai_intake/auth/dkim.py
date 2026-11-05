"""DKIM (RFC 6376): every signature on the stored raw bytes, through dkimpy.

The message is verified exactly as received; a re-serialised copy would break signatures. At most
`MAX_SIGNATURES` signatures are checked, top first. Each yields its `d=` and `s=` and one of:

* `pass` — the body hash and the signature verify against the key in DNS (RSA, 1024 bits or more);
* `fail` — `body_hash_mismatch`, or `signature_mismatch` (a signed header changed);
* `permerror` — `no_key`, `expired` (`x=` passed), `weak_key`, `unsupported_algorithm`
  (ed25519 needs PyNaCl, which is not installed), `bad_signature` (malformed), or
  `body_length_limit`: the signature verifies but carries `l=`, so a part appended after the signed
  length would ride on it. A signature with `l=` never counts;
* `temperror` — the key lookup failed, hit the query cap or the deadline.

Only `pass` counts toward DMARC.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import dkim as dkimpy
from dkim.util import InvalidTagValueList, parse_tag_value

from andai_intake.auth.dns import DnsTempError, Session, run_capped

MAX_SIGNATURES = 8
MIN_KEY_BITS = 1024


@dataclass(frozen=True)
class DkimResult:
    domain: str | None
    selector: str | None
    result: str  # pass | fail | permerror | temperror
    reason: str | None = None

    def summary(self) -> dict[str, Any]:
        return {"d": self.domain, "s": self.selector, "result": self.result, "reason": self.reason}


class _Capture:
    """dkimpy's logger: keeps the error lines (key and DNS problems) to classify a failure."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, message: Any, *args: Any) -> None:
        self.errors.append(str(message))

    def debug(self, *args: Any, **kwargs: Any) -> None:
        pass

    info = warning = debug

    def isEnabledFor(self, level: int) -> bool:  # noqa: N802 - logging.Logger's name
        return level >= logging.ERROR


def _text(value: bytes | None) -> str | None:
    if value is None:
        return None
    return value.decode("utf-8", "replace").strip().rstrip(".").lower() or None


def _tags(header_value: bytes) -> dict[bytes, bytes]:
    try:
        return parse_tag_value(header_value)
    except (InvalidTagValueList, ValueError):
        return {}


def _reason(exc: Exception) -> tuple[str, str]:
    text = str(exc).lower()
    if "body hash mismatch" in text:
        return "fail", "body_hash_mismatch"
    if "x= value is past" in text:
        return "permerror", "expired"
    if "public key too small" in text:
        return "permerror", "weak_key"
    if isinstance(exc, dkimpy.NaClNotFoundError) or "unsupported" in text or "unknown" in text:
        return "permerror", "unsupported_algorithm"
    return "permerror", "bad_signature"


def _verify_one(session: Session, raw: bytes, idx: int, header_value: bytes) -> DkimResult:
    tags = _tags(header_value)
    domain, selector = _text(tags.get(b"d")), _text(tags.get(b"s"))
    capture = _Capture()
    dns_failed: list[str] = []

    def dnsfunc(name: bytes, timeout: float = 5) -> bytes | None:
        try:
            return session.dkim_txt(name)
        except DnsTempError as exc:
            dns_failed.append(str(exc))
            raise dkimpy.DnsTimeoutError(str(exc)) from None

    try:
        verifier = dkimpy.DKIM(raw, logger=capture, minkey=MIN_KEY_BITS, timeout=session.timeout_s)
        ok = verifier.verify(idx, dnsfunc=dnsfunc)
    except dkimpy.DKIMException as exc:
        if dns_failed:
            return DkimResult(domain, selector, "temperror", "dns")
        result, reason = _reason(exc)
        return DkimResult(domain, selector, result, reason)
    except Exception:  # a dkimpy defect on hostile input: no pass
        return DkimResult(domain, selector, "permerror", "bad_signature")
    if ok:
        if b"l" in tags:
            # A length limit leaves the rest of the body unsigned: anyone may append a part (with
            # another meeting link) without breaking the signature.
            return DkimResult(domain, selector, "permerror", "body_length_limit")
        return DkimResult(domain, selector, "pass")
    if dns_failed:
        return DkimResult(domain, selector, "temperror", "dns")
    if any("missing public key" in line for line in capture.errors):
        return DkimResult(domain, selector, "permerror", "no_key")
    if capture.errors:
        return DkimResult(domain, selector, "permerror", "bad_key")
    return DkimResult(domain, selector, "fail", "signature_mismatch")


def check(session: Session, raw: bytes) -> tuple[DkimResult, ...]:
    """Every DKIM signature on `raw` (at most `MAX_SIGNATURES`), top first."""

    def run() -> tuple[DkimResult, ...]:
        try:
            headers = dkimpy.DKIM(raw, logger=_Capture()).headers
        except dkimpy.DKIMException:
            return ()
        signatures = [v for k, v in headers if k.lower() == b"dkim-signature"][:MAX_SIGNATURES]
        return tuple(_verify_one(session, raw, i, v) for i, v in enumerate(signatures))

    try:
        return run_capped(run, session)
    except DnsTempError:
        return (DkimResult(None, None, "temperror", "deadline"),)
