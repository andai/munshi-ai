"""Redacted sender-check results for captured originals, per client family (D-56).

    python -m andai_intake.testing.authreport --capture-record FILE [--arc-sealer DOMAIN ...] DIR

The capture record is JSON:

    {"capture_provider": "google" | "m365" | ...,
     "files": {"O-01-outlook.eml": {"scenario": "O-01", "family": "outlook", ...}, ...}}

`capture_provider` is optional: it names whose ARC seals are the capture hop. For each file the
record lists, the message in DIR is checked as captured by `auth.verdict.verify`, through the
configured resolver and ARC sealers (`IntakeSettings`: `ANDAI_INTAKE_DNS_*`,
`ANDAI_INTAKE_ARC_SEALERS`; `--arc-sealer` adds candidates). The envelope sender comes from
Return-Path. There is no peer address and no HELO: a replay has neither, so SPF is never evaluated
and the verdict rests on DKIM and ARC.

It prints a Markdown table with one row per sample, then a summary per family:

* verdict and mechanism — `dmarc_pass` (`dkim`), `arc_trusted` (`arc`), `not evaluable (replay)`
  or `fail (<reason>)`. `not evaluable (replay)` means DKIM and ARC did not pass, the From domain
  has a DMARC record, and the envelope domain aligns with it. SPF at the original hop could have
  passed, which a replay cannot show, so this is not counted as a rejection of that client family;
* DKIM — each signature's domain class and result. A From-aligned signature that did not verify is
  noted: its key may have been rotated since the capture;
* DMARC — the policy, or `no record` (the sender check fails a From domain without one);
* ARC — the chain result and each set's sealer class, newest first. The capture provider's own
  sealer is marked `capture hop`: that seal describes the capture mailbox, not a notetaker mailbox.
  `would trust` names the newest sealer when allowlisting it would make the message `arc_trusted`.

Nothing that identifies a person or organisation is printed: no addresses, local parts, names,
selectors, ids, subjects or body text. Domains are shown as classes (`member domain`,
`From domain`, `From org`, `other domain`). A platform domain (`sanitise.PLATFORM_DOMAINS`) is shown
as itself, and a tenant under one as `<platform> tenant`. Scenario and family come from the capture
record; a file the record does not list is counted, not read.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from email.parser import BytesHeaderParser
from email.policy import compat32
from email.utils import getaddresses
from pathlib import Path
from typing import Any

from andai_intake.auth import arc, dmarc, spf
from andai_intake.auth.dns import DnsTempError, Session
from andai_intake.auth.verdict import Policy, verify
from andai_intake.replay import wire_bytes
from andai_intake.testing.sanitise import org_domain, platform_of, tenant_label

CAPTURE_SEALERS = {
    "google": "google.com",
    "gmail": "google.com",
    "google workspace": "google.com",
    "m365": "microsoft.com",
    "microsoft": "microsoft.com",
    "microsoft 365": "microsoft.com",
    "exchange online": "microsoft.com",
    "outlook": "microsoft.com",
}
NOT_EVALUABLE = "not evaluable (replay)"


@dataclass(frozen=True)
class Row:
    scenario: str
    family: str
    sender: str
    verdict: str
    mechanism: str
    dkim: str
    dmarc: str
    arc: str
    note: str

    @property
    def category(self) -> str:
        return self.verdict.split(" (", 1)[0] if self.verdict.startswith("fail") else self.verdict


def domain_class(domain: str | None, from_domain: str | None) -> str:
    """How a domain may be shown: a platform as itself, anything else as its relation to From."""
    if not domain:
        return "none"
    domain = domain.lower().rstrip(".")
    tenant = tenant_label(domain)
    if tenant is not None:
        return f"{tenant[1]} tenant"
    platform = platform_of(domain)
    if platform is not None:
        return platform
    if from_domain is None:
        return "other domain"
    if domain == from_domain:
        return "From domain"
    if org_domain(domain) == org_domain(from_domain):
        return "From org"
    return "other domain"


def _headers(raw: bytes) -> Any:
    end = raw.find(b"\r\n\r\n")
    return BytesHeaderParser(policy=compat32).parsebytes(raw[: end + 4] if end != -1 else raw)


def return_path(raw: bytes) -> str | None:
    value = _headers(raw).get("Return-Path")
    if value is None:
        return None
    addresses = [a for _, a in getaddresses([str(value)]) if a]
    return addresses[0] if addresses else None


def arc_sealers(raw: bytes) -> list[tuple[int, str]]:
    """(instance, sealer domain) of every ARC-Seal, newest first."""
    sets = []
    for value in _headers(raw).get_all("ARC-Seal") or []:
        text = re.sub(r"\s+", " ", str(value))
        instance = re.search(r"(?:^|;)\s*i\s*=\s*(\d+)", text)
        sealer = re.search(r"(?:^|;)\s*d\s*=\s*([^;\s]+)", text)
        if instance and sealer:
            sets.append((int(instance.group(1)), sealer.group(1).lower().rstrip(".")))
    return sorted(sets, reverse=True)


def _session(policy: Policy) -> Session:
    return Session(
        policy.resolver,
        max_queries=policy.max_queries,
        deadline_s=policy.deadline_s,
        timeout_s=policy.timeout_s,
    )


def spf_could_align(policy: Policy, from_domain: str, mail_from: str | None) -> bool:
    """Whether SPF at the original hop could have carried DMARC: the From domain has a DMARC
    record and the envelope domain aligns with it."""
    domain, scope = spf.identity(mail_from, None)
    if not domain or scope != "mailfrom":
        return False
    session = _session(policy)
    try:
        record = dmarc.policy(session, from_domain)
        return record is not None and dmarc.aligned(session, domain, from_domain, record.aspf)
    except DnsTempError:
        return False


def evaluate(
    raw: bytes, policy: Policy, *, scenario: str, family: str, capture_sealer: str | None
) -> Row:
    raw = wire_bytes(raw)
    mail_from = return_path(raw)
    auth = verify(raw, mail_from, None, None, policy)
    summary = auth.summary
    from_domain = summary.get("from_domain")
    if from_domain is None:
        reason = summary.get("reason") or "bad_from"
        return Row(scenario, family, "none", f"fail ({reason})", "none", "-", "-", "-", "")

    sender = domain_class(from_domain, None)
    sender = "member domain" if sender == "other domain" else sender
    dmarc_summary = summary["dmarc"]
    notes: list[str] = []

    if auth.sender_auth == "dmarc_pass":
        verdict = "dmarc_pass"
        mechanism = "dkim" if dmarc_summary["dkim_aligned"] else "spf"
    elif auth.sender_auth == "arc_trusted":
        verdict, mechanism = "arc_trusted", "arc"
    elif summary["reason"] != "no_dmarc_record" and spf_could_align(policy, from_domain, mail_from):
        verdict, mechanism = NOT_EVALUABLE, "spf (unknown)"
    else:
        verdict, mechanism = f"fail ({summary['reason']})", "none"

    dkim_parts = []
    for signature in summary["dkim"]:
        cls = domain_class(signature["d"], from_domain)
        text = f"{cls}={signature['result']}"
        if signature["reason"]:
            text += f" ({signature['reason']})"
        dkim_parts.append(text)
        if cls in ("From domain", "From org") and signature["result"] != "pass":
            notes.append(f"aligned DKIM did not verify ({signature['reason'] or 'unknown'})")

    if dmarc_summary["result"] == "none" and dmarc_summary["record"] is None:
        dmarc_text = "no record"
    elif dmarc_summary["result"] == "temperror":
        dmarc_text = "temperror"
    else:
        dmarc_text = f"p={dmarc_summary['policy']}"

    sealers = arc_sealers(raw)
    arc_summary = summary["arc"]
    if not sealers:
        arc_text = "none"
    else:
        shown = []
        for index, (instance, sealer) in enumerate(sealers):
            label = f"i={instance} {domain_class(sealer, from_domain)}"
            if index == 0 and capture_sealer and sealer == capture_sealer:
                label += " (capture hop)"
            shown.append(label)
        arc_text = f"cv={arc_summary['cv']}; " + ", ".join(shown)
        newest = sealers[0][1]
        if not auth.verified and newest not in policy.arc_sealers:
            trial = arc.evaluate(_session(policy), raw, from_domain, frozenset({newest}))
            if trial.trusted:
                arc_text += f"; would trust: {domain_class(newest, from_domain)}"
    return Row(
        scenario,
        family,
        sender,
        verdict,
        mechanism,
        ", ".join(dkim_parts) or "none",
        dmarc_text,
        arc_text,
        "; ".join(notes),
    )


def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")


def render(rows: Iterable[Row], *, unlisted: int = 0, missing: int = 0) -> str:
    rows = list(rows)
    header = ["Scenario", "Family", "From", "Verdict", "Mechanism", "DKIM", "DMARC", "ARC", "Note"]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for row in rows:
        cells = [_cell(str(v)) for v in dataclasses.astuple(row)]
        lines.append("| " + " | ".join(cells) + " |")
    lines += ["", "Per family:"]
    families: dict[str, Counter[str]] = {}
    for row in rows:
        families.setdefault(row.family, Counter())[row.category] += 1
    for family in sorted(families):
        counts = ", ".join(f"{k} {v}" for k, v in sorted(families[family].items()))
        lines.append(f"- {family}: {sum(families[family].values())} sample(s); {counts}")
    lines.append(f"\nSamples: {len(rows)}; listed but missing: {missing}; not listed: {unlisted}")
    return "\n".join(lines)


def load_record(path: Path) -> dict[str, Any]:
    record = json.loads(path.read_text("utf-8"))
    if not isinstance(record, dict) or not isinstance(record.get("files"), dict):
        raise ValueError("the capture record needs a `files` object")
    return record


def report(record: dict[str, Any], directory: Path, policy: Policy) -> str:
    provider = str(record.get("capture_provider") or "").strip().lower()
    capture_sealer = record.get("capture_sealer") or CAPTURE_SEALERS.get(provider)
    files: dict[str, Any] = record["files"]
    rows: list[Row] = []
    missing = 0
    for number, (name, entry) in enumerate(sorted(files.items()), start=1):
        entry = entry if isinstance(entry, dict) else {}
        path = directory / Path(name).name
        if not path.is_file():
            missing += 1
            continue
        rows.append(
            evaluate(
                path.read_bytes(),
                policy,
                scenario=str(entry.get("scenario") or f"sample {number}"),
                family=str(entry.get("family") or "unknown"),
                capture_sealer=capture_sealer,
            )
        )
    listed = {Path(name).name for name in files}
    unlisted = sum(1 for p in directory.glob("*.eml") if p.name not in listed)
    return render(rows, unlisted=unlisted, missing=missing)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m andai_intake.testing.authreport",
        description="Redacted sender-check results for captured originals.",
    )
    parser.add_argument("--capture-record", required=True, type=Path)
    parser.add_argument(
        "--arc-sealer", action="append", default=[], help="candidate sealer to also trust"
    )
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    try:
        record = load_record(args.capture_record)
    except (OSError, ValueError) as exc:
        parser.error(f"capture record: {type(exc).__name__}")
    from andai_intake.auth.verdict import configured_policy

    policy = configured_policy()
    extra = frozenset(d.strip().rstrip(".").lower() for d in args.arc_sealer if d.strip())
    if extra:
        policy = dataclasses.replace(policy, arc_sealers=policy.arc_sealers | extra)
    print(report(record, args.directory, policy))
    return 0


if __name__ == "__main__":
    sys.exit(main())
