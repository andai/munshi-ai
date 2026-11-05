"""ARC (RFC 8617): when a forwarder's seal may stand in for a DMARC pass that forwarding broke.

`dkim.arc_verify` validates the chain on the raw bytes and yields `cv` (`pass`, `fail` or `none`; a
chain that an earlier hop marked failed is `fail`). A message is trusted through ARC only when all
of these hold:

1. `cv` is `pass`;
2. the `d=` of the highest-instance ARC-Seal is in `ANDAI_INTAKE_ARC_SEALERS` (exact domains,
   lower-case; the list ships empty, and changing it needs the CTO);
3. that hop's ARC-Authentication-Results (parsed with authres) shows `dmarc=pass` with
   `header.from` equal to the message's From domain, and no other DMARC result for that domain.

Nothing else in the message is read: an `Authentication-Results` header without a valid seal from an
allowlisted sealer is ignored, whatever it claims. ARC is being moved to Historic, so this is kept as
one replaceable policy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import authres
import dkim as dkimpy

from andai_intake.auth.dns import DnsTempError, Session, run_capped

_INSTANCE = re.compile(rb"^\s*i\s*=\s*\d+\s*;\s*", re.IGNORECASE)


@dataclass(frozen=True)
class ArcResult:
    cv: str  # pass | fail | none | temperror
    sealer: str | None
    instances: int
    trusted: bool
    reason: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "cv": self.cv,
            "sealer": self.sealer,
            "instances": self.instances,
            "trusted": self.trusted,
            "reason": self.reason,
        }


def _decode(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return str(value).strip().rstrip(".").lower() or None


def aar_dmarc_pass(aar_value: bytes, from_domain: str) -> bool:
    """Whether an ARC-Authentication-Results value records `dmarc=pass` for `from_domain` (and no
    other DMARC result for it)."""
    value = _INSTANCE.sub(b"", aar_value, count=1).decode("utf-8", "replace").strip()
    try:
        header = authres.AuthenticationResultsHeader.parse_value(value)
    except Exception:  # authres raises its own SyntaxError and others on hostile input
        return False
    results = [
        r
        for r in header.results
        if str(getattr(r, "method", "")).lower() == "dmarc"
        and any(
            p.type == "header" and p.name == "from" and _decode(p.value) == from_domain
            for p in r.properties
        )
    ]
    return bool(results) and all(str(r.result).lower() == "pass" for r in results)


def evaluate(session: Session, raw: bytes, from_domain: str, sealers: frozenset[str]) -> ArcResult:
    def dnsfunc(name: bytes, timeout: float = 5) -> bytes | None:
        try:
            return session.dkim_txt(name)
        except DnsTempError as exc:
            raise dkimpy.DnsTimeoutError(str(exc)) from None

    def run() -> tuple[Any, list[dict[str, Any]], str]:
        return dkimpy.arc_verify(raw, dnsfunc=dnsfunc, minkey=1024)

    try:
        cv_raw, results, reason = run_capped(run, session)
    except DnsTempError:
        return ArcResult("temperror", None, 0, False, "deadline")
    except Exception:  # a dkimpy defect on hostile input: not trusted
        return ArcResult("fail", None, 0, False, "bad_chain")

    instances = len(results)
    sealer = _decode(results[0].get("as-domain")) if results else None
    cv = _decode(cv_raw) or "fail"  # None: an earlier hop ended the chain
    if cv == "none":
        return ArcResult("none", None, 0, False)
    if cv != "pass":
        return ArcResult("fail", sealer, instances, False, "chain_invalid")
    if sealer not in sealers:
        return ArcResult("pass", sealer, instances, False, "sealer_not_allowlisted")
    if not aar_dmarc_pass(results[0].get("aar-value") or b"", from_domain):
        return ArcResult("pass", sealer, instances, False, "no_dmarc_pass_at_sealer")
    return ArcResult("pass", sealer, instances, True)
