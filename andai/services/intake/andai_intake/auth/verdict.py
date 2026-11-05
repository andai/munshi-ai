"""The sender verdict for one stored message (D-56, INV-03).

`verify(raw, mail_from, peer_ip, helo)` returns a `SenderAuth`:

* `dmarc_pass` — the From domain has a DMARC record, and SPF (MAIL FROM) or a DKIM signature passed
  and aligns with it (`dmarc.py`);
* `arc_trusted` — DMARC did not pass, but a valid ARC chain's newest seal is from an allowlisted
  sealer whose hop recorded `dmarc=pass` for the From domain (`arc.py`);
* `fail` — anything else: no or several From addresses, no alignment, no DMARC record, a DNS
  `temperror`, the query cap or the deadline. A failed message is quarantined `sender_unverified`.

`sender` is the one From address, lower-cased, or None when the From header does not name exactly
one. It is reported even on `fail`, so the quarantine row can be attributed (D-58); it is
authenticated only when `verified`. `summary` is the check summary stored on the `inbound_mail` row
(`auth_summary`). It holds domains, selectors and results, never a local part or message content.

All lookups of one message share a `dns.Session` (`ANDAI_INTAKE_DNS_MAX_QUERIES`, a 20-second
deadline). The resolver comes from settings unless a `Policy` is passed in, as tests and the 07-08
report tool do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from email.parser import BytesHeaderParser
from email.policy import compat32
from email.utils import getaddresses
from functools import lru_cache
from typing import Any

from andai_intake.auth import arc, dkim, dmarc, spf
from andai_intake.auth.dns import DnsResolver, Resolver, Session

SENDER_AUTH = ("dmarc_pass", "arc_trusted", "fail")
DEADLINE_S = 20.0  # RFC 7208 4.6.4 suggests at least 20 s for SPF alone; this is the whole message
SENDER_MAX = 320


@dataclass(frozen=True)
class Policy:
    resolver: Resolver
    arc_sealers: frozenset[str] = frozenset()
    max_queries: int = 30
    timeout_s: float = 3.0
    deadline_s: float = DEADLINE_S

    @classmethod
    def from_settings(cls, settings: Any) -> Policy:
        return cls(
            resolver=DnsResolver(settings.dns_nameservers, timeout_s=settings.intake_dns_timeout_s),
            arc_sealers=settings.arc_sealers,
            max_queries=settings.intake_dns_max_queries,
            timeout_s=settings.intake_dns_timeout_s,
        )


@lru_cache(maxsize=1)
def configured_policy() -> Policy:
    from andai_intake.settings import get_intake_settings

    return Policy.from_settings(get_intake_settings())


@dataclass(frozen=True)
class SenderAuth:
    sender_auth: str  # dmarc_pass | arc_trusted | fail
    sender: str | None
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.sender_auth in ("dmarc_pass", "arc_trusted")

    @property
    def quarantine_reason(self) -> str | None:
        return None if self.verified else "sender_unverified"


def _header_block(raw: bytes) -> bytes:
    for separator in (b"\r\n\r\n", b"\n\n"):
        end = raw.find(separator)
        if end != -1:
            return raw[: end + len(separator)]
    return raw


def _ascii_domain(domain: str) -> str | None:
    domain = domain.strip().rstrip(".").lower()
    if not domain or domain.startswith("[") or ".." in domain or "." not in domain:
        return None
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if any(not label or len(label) > 63 for label in domain.split(".")) or len(domain) > 253:
        return None
    return domain


def from_address(raw: bytes) -> tuple[str | None, str | None, str | None]:
    """(address, domain, problem): the single RFC5322.From address, or a problem
    (`no_from`, `several_from`, `bad_from`)."""
    headers = BytesHeaderParser(policy=compat32).parsebytes(_header_block(raw))
    values = headers.get_all("From") or []
    if not values:
        return None, None, "no_from"
    if len(values) > 1:
        return None, None, "several_from"
    addresses = [addr for _, addr in getaddresses([str(values[0])]) if addr]
    if len(addresses) != 1:
        return None, None, "several_from" if len(addresses) > 1 else "bad_from"
    local, at, domain = addresses[0].rpartition("@")
    ascii_domain = _ascii_domain(domain) if at else None
    if not local or ascii_domain is None:
        return None, None, "bad_from"
    address = f"{local}@{ascii_domain}".lower()
    if len(address) > SENDER_MAX or any(c.isspace() or ord(c) < 32 for c in address):
        return None, None, "bad_from"
    return address, ascii_domain, None


def verify(
    raw: bytes,
    mail_from: str | None,
    peer_ip: str | None,
    helo: str | None,
    policy: Policy | None = None,
) -> SenderAuth:
    policy = policy or configured_policy()
    sender, from_domain, problem = from_address(raw)
    if problem is not None or from_domain is None:
        return SenderAuth("fail", None, {"verdict": "fail", "reason": problem})

    session = Session(
        policy.resolver,
        max_queries=policy.max_queries,
        deadline_s=policy.deadline_s,
        timeout_s=policy.timeout_s,
    )
    spf_result = spf.check(session, peer_ip, mail_from, helo)
    dkim_results = dkim.check(session, raw)
    dmarc_result = dmarc.evaluate(session, from_domain, spf_result, dkim_results)
    arc_result = arc.evaluate(session, raw, from_domain, policy.arc_sealers)

    if dmarc_result.result == "pass":
        verdict, reason = "dmarc_pass", None
    elif arc_result.trusted:
        verdict, reason = "arc_trusted", None
    elif dmarc_result.result == "none":
        verdict, reason = "fail", "no_dmarc_record"
    elif dmarc_result.result == "temperror" or session.temperror:
        verdict, reason = "fail", "temperror"
    else:
        verdict, reason = "fail", "not_aligned"

    summary = {
        "verdict": verdict,
        "reason": reason,
        "from_domain": from_domain,
        "spf": spf_result.summary(),
        "dkim": [d.summary() for d in dkim_results],
        "dmarc": dmarc_result.summary(),
        "arc": arc_result.summary(),
        "dns": session.summary(),
    }
    return SenderAuth(verdict, sender, summary)
