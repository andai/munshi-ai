"""DMARC (RFC 9989): policy discovery, organizational domains and identifier alignment.

There is no public-suffix list; RFC 9989's DNS tree walk takes its place. From the subject domain,
`_dmarc.<target>` is asked at each step. A name of 8 or more labels is first cut to its last 7, then
one label is dropped at a time, so a walk asks at most 8 names. Answers are memoised in the
message's session.

* The policy record is the first single valid `v=DMARC1` record the walk finds from the From domain.
  Several valid records at one name count as none.
* The organizational domain of a name comes from a full walk. The first record with `psd=n` names it;
  the first with `psd=y` makes it the name one label below. Otherwise it is the name with the fewest
  labels that has a record, or the name itself when none has. A one-label name (a TLD) is never an
  organizational domain: a TLD record without `psd=y` would otherwise align every domain under it.
* Alignment: strict (`s`) needs the identifier to equal the From domain. Relaxed (`r`, the default)
  needs equal organizational domains. Two names that share fewer than two trailing labels cannot
  align, so they are rejected without lookups.

`evaluate` is `pass` when the SPF identity (MAIL FROM only, not HELO) passed and aligns under
`aspf`, or some passing DKIM `d=` aligns under `adkim`. A From domain without a policy record is
`none`. A failed policy lookup is `temperror`, and so is a failed alignment lookup when nothing else
aligns. `p=none` still needs alignment for `pass`. Alignment is also
worked out without a record (relaxed), so the summary can tell an admin that only the DMARC record is
missing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from andai_intake.auth.dkim import DkimResult
from andai_intake.auth.dns import DnsTempError, Session
from andai_intake.auth.spf import SpfResult

MAX_LABELS_KEPT = 7
POLICIES = ("none", "quarantine", "reject")


@dataclass(frozen=True)
class DmarcRecord:
    domain: str  # where it was found
    p: str
    adkim: str
    aspf: str
    psd: str  # y | n | u


@dataclass(frozen=True)
class DmarcResult:
    result: str  # pass | fail | none | temperror
    record_domain: str | None
    policy: str | None
    spf_aligned: bool
    dkim_aligned: tuple[str, ...]

    def summary(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "record": self.record_domain,
            "policy": self.policy,
            "spf_aligned": self.spf_aligned,
            "dkim_aligned": list(self.dkim_aligned),
        }


def parse_record(domain: str, value: bytes) -> DmarcRecord | None:
    """A DMARC record, or None unless its first tag is `v=DMARC1`."""
    try:
        text = value.decode("ascii")
    except UnicodeDecodeError:
        return None
    tags: dict[str, str] = {}
    for index, part in enumerate(p.strip() for p in text.split(";")):
        if not part:
            continue
        name, sep, tag_value = part.partition("=")
        name, tag_value = name.strip().lower(), tag_value.strip()
        if index == 0 and (not sep or name != "v" or tag_value != "DMARC1"):
            return None
        tags.setdefault(name, tag_value)
    if tags.get("v") != "DMARC1":
        return None
    p = tags.get("p", "").lower()
    return DmarcRecord(
        domain=domain,
        p=p if p in POLICIES else "none",
        adkim="s" if tags.get("adkim", "").lower() == "s" else "r",
        aspf="s" if tags.get("aspf", "").lower() == "s" else "r",
        psd=tags.get("psd", "u").lower() if tags.get("psd", "u").lower() in ("y", "n") else "u",
    )


def walk_targets(domain: str) -> list[str]:
    """The names a tree walk from `domain` asks about, in order (at most 8)."""
    labels = [label for label in domain.lower().rstrip(".").split(".") if label]
    targets = [".".join(labels)] if labels else []
    while len(labels) > 1:
        labels = labels[-MAX_LABELS_KEPT:] if len(labels) >= 8 else labels[1:]
        targets.append(".".join(labels))
    return targets


def record_at(session: Session, domain: str) -> DmarcRecord | None:
    records = [
        record
        for record in (parse_record(domain, v) for v in session.txt(f"_dmarc.{domain}"))
        if record is not None
    ]
    return records[0] if len(records) == 1 else None


def policy(session: Session, from_domain: str) -> DmarcRecord | None:
    for target in walk_targets(from_domain):
        record = record_at(session, target)
        if record is not None:
            return record
    return None


def org_domain(session: Session, domain: str) -> str:
    domain = domain.lower().rstrip(".")
    found: list[DmarcRecord] = []
    for target in walk_targets(domain):
        record = record_at(session, target)
        if record is None:
            continue
        if record.psd == "n":
            return _not_a_tld(target, domain)
        if record.psd == "y":
            labels = domain.split(".")
            below = len(target.split(".")) + 1
            return ".".join(labels[-below:]) if below <= len(labels) else domain
        found.append(record)
    if not found:
        return domain
    shortest = min(found, key=lambda r: len(r.domain.split(".")))
    return _not_a_tld(shortest.domain, domain)


def _not_a_tld(candidate: str, domain: str) -> str:
    if "." in candidate:
        return candidate
    labels = domain.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else domain


def _common_labels(a: str, b: str) -> int:
    count = 0
    for x, y in zip(reversed(a.split(".")), reversed(b.split(".")), strict=False):
        if x != y:
            break
        count += 1
    return count


def aligned(session: Session, identifier: str, from_domain: str, mode: str) -> bool:
    identifier = identifier.lower().rstrip(".")
    from_domain = from_domain.lower().rstrip(".")
    if identifier == from_domain:
        return True
    if mode == "s" or _common_labels(identifier, from_domain) < 2:
        return False
    return org_domain(session, identifier) == org_domain(session, from_domain)


def evaluate(
    session: Session, from_domain: str, spf: SpfResult, dkims: Sequence[DkimResult]
) -> DmarcResult:
    try:
        record = policy(session, from_domain)
    except DnsTempError:
        return DmarcResult("temperror", None, None, False, ())
    aspf = record.aspf if record else "r"
    adkim = record.adkim if record else "r"
    lookup_failed = False

    def check(identifier: str, mode: str) -> bool:
        # One identifier's failed lookup must not hide another identifier's alignment.
        nonlocal lookup_failed
        try:
            return aligned(session, identifier, from_domain, mode)
        except DnsTempError:
            lookup_failed = True
            return False

    spf_aligned = bool(
        spf.result == "pass" and spf.scope == "mailfrom" and spf.domain
        and check(spf.domain, aspf)
    )
    dkim_aligned = tuple(
        sorted(
            {d.domain for d in dkims if d.result == "pass" and d.domain and check(d.domain, adkim)}
        )
    )
    if record is None:
        result = "none"
    elif spf_aligned or dkim_aligned:
        result = "pass"
    else:
        result = "temperror" if lookup_failed else "fail"
    return DmarcResult(
        result,
        record.domain if record else None,
        record.p if record else None,
        spf_aligned,
        dkim_aligned,
    )
