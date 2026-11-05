"""SPF (RFC 7208) of the delivering peer, through pyspf.

The identity checked is the MAIL FROM domain, or the HELO name when MAIL FROM is null (`<>`). The
result is one of pyspf's: `pass`, `fail`, `softfail`, `neutral`, `none`, `permerror` or `temperror`.
pyspf enforces RFC 7208's limits itself: 10 DNS-querying mechanisms and 2 void lookups, beyond which
the result is `permerror`. Its lookups go to the message's `dns.Session`, so they count against the
query cap and stop at the deadline (`temperror`).

pyspf calls a module-level `DNSLookup`. This module points that hook, once, at a dispatcher that
answers from the session of the check running in the current context. A check outside a session
gets `temperror`; it never falls through to the system resolver.
"""

from __future__ import annotations

import contextvars
import warnings
from dataclasses import dataclass
from typing import Any

from andai_intake.auth.dns import DnsTempError, Session, run_capped

with warnings.catch_warnings():  # pyspf's docstrings carry invalid escapes
    warnings.simplefilter("ignore", SyntaxWarning)
    import spf as pyspf

RESULTS = ("pass", "fail", "softfail", "neutral", "none", "permerror", "temperror")

_session: contextvars.ContextVar[Session | None] = contextvars.ContextVar(
    "andai_intake_spf_session", default=None
)


def _lookup(name: str, qtype: str, strict: Any = True, timeout: float = 30) -> list:
    session = _session.get()
    if session is None:
        raise pyspf.TempError("DNS: no resolver session for this check")
    try:
        return session.spf_records(name, qtype)
    except DnsTempError as exc:
        raise pyspf.TempError(f"DNS: {exc}") from None


pyspf.DNSLookup = _lookup


@dataclass(frozen=True)
class SpfResult:
    result: str
    domain: str | None
    scope: str  # "mailfrom" or "helo"

    def summary(self) -> dict[str, Any]:
        return {"result": self.result, "domain": self.domain, "scope": self.scope}


def identity(mail_from: str | None, helo: str | None) -> tuple[str | None, str]:
    """The domain SPF checks and its scope, as pyspf derives them."""
    mail_from = (mail_from or "").strip().strip("<>")
    helo_domain = (helo or "").strip().rstrip(".").lower() or None
    if not mail_from:
        return helo_domain, "helo"
    local, at, domain = mail_from.rpartition("@")
    if not at:
        return helo_domain, "mailfrom"
    return domain.rstrip(".").lower() or None, "mailfrom"


def check(
    session: Session, peer_ip: str | None, mail_from: str | None, helo: str | None
) -> SpfResult:
    domain, scope = identity(mail_from, helo)
    if not peer_ip or not domain:
        return SpfResult("none", domain, scope)
    sender = (mail_from or "").strip().strip("<>")

    def run() -> str:
        token = _session.set(session)
        try:
            result, _explanation = pyspf.check2(
                i=peer_ip,
                s=sender,
                h=(helo or "").strip(),
                timeout=session.timeout_s,
                querytime=0,  # the session's deadline bounds the whole check
            )
        finally:
            _session.reset(token)
        return str(result)

    try:
        result = run_capped(run, session)
    except DnsTempError:
        return SpfResult("temperror", domain, scope)
    except Exception:  # a pyspf defect on hostile input: no pass
        return SpfResult("permerror", domain, scope)
    if result not in RESULTS:
        result = "permerror"
    return SpfResult(result, domain, scope)
