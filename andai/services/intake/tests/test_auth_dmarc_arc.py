"""DMARC alignment by the RFC 9989 tree walk, the ARC trust rule and the verdict, including the
spoofed sender, on stub DNS. No network: the library default resolvers are blocked."""

from __future__ import annotations

import json

import dns.resolver
import pytest

import mailkit
from andai_intake.auth import dmarc
from andai_intake.auth.dns import Session, StubResolver
from andai_intake.auth.verdict import Policy, SenderAuth, from_address, verify

DOMAIN = "a.example.test"
MEMBER = f"member@{DOMAIN}"
PEER = "192.0.2.25"
FORWARDER_IP = "198.51.100.40"
SEALER = "fwd.example.net"


@pytest.fixture(autouse=True)
def no_system_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError("a library fell back to the system resolver")

    monkeypatch.setattr(dns.resolver, "resolve", refuse)
    monkeypatch.setattr(dns.resolver, "query", refuse)


@pytest.fixture
def kit() -> mailkit.Kit:
    kit = mailkit.Kit()
    kit.dmarc(DOMAIN, p="reject")
    kit.spf(DOMAIN, "ip4:192.0.2.0/24")
    return kit


def _verify(
    kit: mailkit.Kit,
    raw: bytes,
    *,
    mail_from: str = f"bounce@{DOMAIN}",
    peer: str = PEER,
    sealers: frozenset[str] = frozenset(),
    max_queries: int = 30,
) -> SenderAuth:
    policy = Policy(resolver=kit.zone, arc_sealers=sealers, max_queries=max_queries)
    return verify(raw, mail_from, peer, f"mx.{DOMAIN}", policy)


def _dmarc_queries(zone: StubResolver) -> list[str]:
    return [name for name, rdtype in zone.queries if name.startswith("_dmarc.")]


# ─── the tree walk ────────────────────────────────────────────────────────────────────────────────


def test_walk_targets_follow_rfc_9989() -> None:
    assert dmarc.walk_targets("a.example.test") == ["a.example.test", "example.test", "test"]
    long = "l1.l2.l3.l4.l5.l6.l7.l8.l9.example.test"  # 11 labels: cut to 7, then one at a time
    targets = dmarc.walk_targets(long)
    assert targets[:3] == [long, "l5.l6.l7.l8.l9.example.test", "l6.l7.l8.l9.example.test"]
    assert len(targets) == 8 and targets[-1] == "test"
    assert all(len(dmarc.walk_targets(n)) <= 8 for n in (long, "x." * 20 + "test"))


def test_parse_record() -> None:
    record = dmarc.parse_record("d.test", b"v=DMARC1; p=quarantine; adkim=s; aspf=s; psd=n")
    assert (record.p, record.adkim, record.aspf, record.psd) == ("quarantine", "s", "s", "n")
    assert dmarc.parse_record("d.test", b"v=DMARC1").p == "none"  # p missing → none
    assert dmarc.parse_record("d.test", b"v=DMARC1; p=bogus; adkim=x").adkim == "r"
    assert dmarc.parse_record("d.test", b"p=reject; v=DMARC1") is None  # v= must come first
    assert dmarc.parse_record("d.test", b"v=spf1 -all") is None


def test_policy_for_a_subdomain_is_found_by_the_walk_in_at_most_8_queries(
    kit: mailkit.Kit,
) -> None:
    sender = "member@x.y.z.a.example.test"
    raw = kit.dkim(mailkit.message(sender), DOMAIN)
    result = _verify(kit, raw, mail_from="", peer="")
    assert result.sender_auth == "dmarc_pass"
    assert result.summary["dmarc"]["record"] == DOMAIN
    assert result.summary["dmarc"]["dkim_aligned"] == [DOMAIN]
    assert len(set(_dmarc_queries(kit.zone))) <= 8


def test_several_dmarc_records_count_as_none(kit: mailkit.Kit) -> None:
    kit.zone.txt(f"_dmarc.{DOMAIN}", "v=DMARC1; p=none")
    raw = kit.dkim(mailkit.message(MEMBER), DOMAIN)
    result = _verify(kit, raw)
    assert (result.sender_auth, result.summary["reason"]) == ("fail", "no_dmarc_record")


# ─── alignment ────────────────────────────────────────────────────────────────────────────────────


def test_relaxed_dkim_alignment_of_a_subdomain_signer(kit: mailkit.Kit) -> None:
    raw = kit.dkim(mailkit.message(MEMBER), f"mail.{DOMAIN}")
    result = _verify(kit, raw, mail_from="bounce@esp.example.net", peer="203.0.113.9")
    assert result.sender_auth == "dmarc_pass"
    assert result.summary["dmarc"]["dkim_aligned"] == [f"mail.{DOMAIN}"]
    assert result.summary["dmarc"]["spf_aligned"] is False


def test_strict_dkim_alignment_needs_the_exact_domain(kit: mailkit.Kit) -> None:
    kit.dmarc(DOMAIN, p="reject", adkim="s")
    raw = kit.dkim(mailkit.message(MEMBER), f"mail.{DOMAIN}")
    result = _verify(kit, raw, mail_from="bounce@esp.example.net", peer="203.0.113.9")
    assert (result.sender_auth, result.summary["reason"]) == ("fail", "not_aligned")
    raw = kit.dkim(mailkit.message(MEMBER), DOMAIN)
    assert _verify(kit, raw, mail_from="bounce@esp.example.net").sender_auth == "dmarc_pass"


def test_spf_alignment_relaxed_and_strict(kit: mailkit.Kit) -> None:
    kit.spf(f"bounce.{DOMAIN}", "ip4:192.0.2.0/24")
    raw = mailkit.message(MEMBER)  # no DKIM at all
    assert _verify(kit, raw, mail_from=f"x@bounce.{DOMAIN}").sender_auth == "dmarc_pass"
    kit.dmarc(DOMAIN, p="reject", aspf="s")
    assert _verify(kit, raw, mail_from=f"x@bounce.{DOMAIN}").sender_auth == "fail"
    assert _verify(kit, raw, mail_from=f"x@{DOMAIN}").sender_auth == "dmarc_pass"
    # SPF from the wrong peer does not pass, so nothing aligns.
    assert _verify(kit, raw, mail_from=f"x@{DOMAIN}", peer="203.0.113.9").sender_auth == "fail"


def test_a_null_mail_from_gives_no_spf_alignment(kit: mailkit.Kit) -> None:
    kit.spf(f"mx.{DOMAIN}", "ip4:192.0.2.0/24")
    result = _verify(kit, mailkit.message(MEMBER), mail_from="")
    assert result.summary["spf"] == {"result": "pass", "domain": f"mx.{DOMAIN}", "scope": "helo"}
    assert result.sender_auth == "fail"


def test_p_none_still_needs_alignment(kit: mailkit.Kit) -> None:
    kit.dmarc(DOMAIN, p="none")
    raw = kit.dkim(mailkit.message(MEMBER), "attacker.test")
    result = _verify(kit, raw, mail_from="x@attacker.test", peer="203.0.113.9")
    assert result.sender_auth == "fail"
    assert result.summary["dmarc"]["policy"] == "none"
    raw = kit.dkim(mailkit.message(MEMBER), DOMAIN)
    assert _verify(kit, raw, peer="203.0.113.9").sender_auth == "dmarc_pass"


def test_no_dmarc_record_fails_even_with_an_aligned_dkim_pass(kit: mailkit.Kit) -> None:
    kit.zone.remove(f"_dmarc.{DOMAIN}")
    raw = kit.dkim(mailkit.message(MEMBER), DOMAIN)
    result = _verify(kit, raw)
    assert (result.sender_auth, result.summary["reason"]) == ("fail", "no_dmarc_record")
    # The summary still shows the admin that only the DMARC record is missing.
    assert result.summary["dmarc"] == {
        "result": "none", "record": None, "policy": None,
        "spf_aligned": True, "dkim_aligned": [DOMAIN],
    }
    assert result.quarantine_reason == "sender_unverified"


def test_org_domains_from_psd_and_shortest_records(kit: mailkit.Kit) -> None:
    session = Session(kit.zone)
    assert dmarc.org_domain(session, f"x.{DOMAIN}") == DOMAIN
    assert not dmarc.aligned(session, "b.example.test", DOMAIN, "r")
    # A record at example.test without psd makes it the organizational domain of both.
    kit.dmarc("example.test", p="none")
    session = Session(kit.zone)
    assert dmarc.org_domain(session, f"x.{DOMAIN}") == "example.test"
    assert dmarc.aligned(session, "b.example.test", DOMAIN, "r")
    # psd=y marks example.test as a public suffix: the org domain is one label below it.
    kit.dmarc("example.test", p="none", psd="y")
    session = Session(kit.zone)
    assert dmarc.org_domain(session, f"x.{DOMAIN}") == DOMAIN
    assert not dmarc.aligned(session, "b.example.test", DOMAIN, "r")
    # psd=n names the org domain directly.
    kit.zone.remove("_dmarc.example.test")
    kit.dmarc(f"x.{DOMAIN}", p="none", psd="n")
    assert dmarc.org_domain(Session(kit.zone), f"y.x.{DOMAIN}") == f"x.{DOMAIN}"


def test_a_tld_record_never_becomes_the_organizational_domain(kit: mailkit.Kit) -> None:
    kit.dmarc("test", p="none")
    session = Session(kit.zone)
    assert dmarc.org_domain(session, "b.example.test") == "example.test"
    raw = kit.dkim(mailkit.message(MEMBER), "attacker.test")
    result = _verify(kit, raw, mail_from="x@attacker.test", peer="203.0.113.9")
    assert result.sender_auth == "fail"


def test_google_signature_does_not_align_with_a_customer_domain(kit: mailkit.Kit) -> None:
    """A Google Calendar invite without the customer's own DKIM: envelope at
    calendar-server.bounces.google.com, DKIM d=google.com only (D-56: quarantined)."""
    kit.spf("calendar-server.bounces.google.com", "ip4:209.85.0.0/16")
    kit.dmarc("google.com", p="reject")
    raw = kit.dkim(mailkit.message(MEMBER), "google.com")
    result = _verify(
        kit, raw, mail_from="abc@calendar-server.bounces.google.com", peer="209.85.1.1"
    )
    assert result.summary["spf"]["result"] == "pass"
    assert result.summary["dkim"][0]["result"] == "pass"
    assert (result.sender_auth, result.summary["reason"]) == ("fail", "not_aligned")
    # With the customer's own DKIM as well, it passes.
    raw = kit.dkim(raw, DOMAIN)
    assert _verify(
        kit, raw, mail_from="abc@calendar-server.bounces.google.com", peer="209.85.1.1"
    ).sender_auth == "dmarc_pass"


# ─── the spoofed sender and forged headers ───────────────────────────────────────────────────────


def test_spoofed_sender_is_quarantined_sender_unverified(kit: mailkit.Kit) -> None:
    """A real member's From, a valid DKIM signature by the attacker's domain and SPF pass for the
    attacker's envelope: nothing aligns with the member's domain."""
    kit.dmarc("attacker.test", p="none")
    kit.spf("attacker.test", "ip4:203.0.113.0/24")
    raw = kit.dkim(mailkit.message(MEMBER, subject="Board meeting"), "attacker.test")
    result = _verify(kit, raw, mail_from="ceo@attacker.test", peer="203.0.113.66")
    assert result.summary["spf"]["result"] == "pass"
    assert [d["result"] for d in result.summary["dkim"]] == ["pass"]
    assert result.sender_auth == "fail"
    assert result.verified is False
    assert result.quarantine_reason == "sender_unverified"
    assert result.sender == MEMBER  # reported for attribution, not authenticated


def test_forged_authentication_results_without_arc_are_ignored(kit: mailkit.Kit) -> None:
    raw = mailkit.message(
        MEMBER,
        headers=(
            ("Authentication-Results",
             f"mx.microsoft.com 1; spf=pass smtp.mailfrom={DOMAIN}; dkim=pass header.d={DOMAIN};"
             f" dmarc=pass action=none header.from={DOMAIN}; compauth=pass reason=100"),
            ("X-MS-Exchange-Organization-AuthAs", "Internal"),
            ("ARC-Authentication-Results",
             f"i=1; mx.microsoft.com 1; dmarc=pass header.from={DOMAIN}"),
        ),
    )
    result = _verify(kit, raw, mail_from="x@attacker.test", peer="203.0.113.66",
                     sealers=frozenset({"microsoft.com", "mx.microsoft.com"}))
    assert result.sender_auth == "fail"
    assert result.summary["arc"]["trusted"] is False


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ("To: x@y.test\r\n", "no_from"),
        (f"From: {MEMBER}\r\nFrom: other@b.example.test\r\n", "several_from"),
        (f"From: {MEMBER}, other@b.example.test\r\n", "several_from"),
        ("From: undisclosed-recipients:;\r\n", "bad_from"),
        ("From: someone@localhost\r\n", "bad_from"),
    ],
)
def test_exactly_one_from_address_is_required(kit: mailkit.Kit, headers: str, reason: str) -> None:
    raw = (headers + "Subject: x\r\n\r\nbody\r\n").encode()
    result = _verify(kit, raw)
    assert (result.sender_auth, result.sender, result.summary["reason"]) == ("fail", None, reason)
    assert kit.zone.queries == []  # nothing is looked up for a message without one sender


def test_from_address_is_lower_cased_and_display_names_are_ignored() -> None:
    raw = b'From: "member@a.example.test" <Boss@B.Example.Test>\r\nSubject: x\r\n\r\n'
    assert from_address(raw) == ("boss@b.example.test", "b.example.test", None)


# ─── ARC ──────────────────────────────────────────────────────────────────────────────────────────


def _forwarded(kit: mailkit.Kit, results: str, sealer: str = SEALER) -> bytes:
    """The member's signed invite, changed by a forwarder (DKIM breaks), then sealed by it."""
    signed = kit.dkim(mailkit.message(MEMBER), DOMAIN)
    changed = mailkit.append_to_body(signed, "-- \r\nSent through a list\r\n")
    return kit.arc(changed, sealer, results)


def _via_forwarder(kit: mailkit.Kit, raw: bytes, sealers: frozenset[str]) -> SenderAuth:
    kit.spf(SEALER, f"ip4:{FORWARDER_IP}")
    return _verify(kit, raw, mail_from=f"bounce@{SEALER}", peer=FORWARDER_IP, sealers=sealers)


def test_arc_from_an_allowlisted_sealer_with_dmarc_pass_is_trusted(kit: mailkit.Kit) -> None:
    raw = _forwarded(kit, f"dkim=pass header.d={DOMAIN}; dmarc=pass header.from={DOMAIN}")
    result = _via_forwarder(kit, raw, frozenset({SEALER}))
    assert result.summary["dkim"][0]["reason"] == "body_hash_mismatch"
    assert result.summary["dmarc"]["result"] == "fail"
    assert result.summary["arc"] == {
        "cv": "pass", "sealer": SEALER, "instances": 1, "trusted": True, "reason": None,
    }
    assert result.sender_auth == "arc_trusted"
    assert result.verified


def test_arc_from_a_sealer_not_on_the_allowlist_is_not_trusted(kit: mailkit.Kit) -> None:
    raw = _forwarded(kit, f"dmarc=pass header.from={DOMAIN}")
    result = _via_forwarder(kit, raw, frozenset())  # the shipped default: empty
    assert result.summary["arc"]["reason"] == "sealer_not_allowlisted"
    assert result.sender_auth == "fail"
    result = _via_forwarder(kit, raw, frozenset({"other-forwarder.example.net"}))
    assert result.sender_auth == "fail"


def test_arc_hop_must_record_dmarc_pass_for_the_from_domain(kit: mailkit.Kit) -> None:
    sealers = frozenset({SEALER})
    for results in (
        f"dkim=pass header.d={DOMAIN}",
        f"dmarc=fail header.from={DOMAIN}",
        "dmarc=pass header.from=b.example.test",
        f"dmarc=pass header.from={DOMAIN}; dmarc=fail header.from={DOMAIN}",
    ):
        result = _via_forwarder(kit, _forwarded(kit, results), sealers)
        assert result.summary["arc"]["reason"] == "no_dmarc_pass_at_sealer", results
        assert result.sender_auth == "fail", results


def test_broken_arc_chain_is_not_trusted(kit: mailkit.Kit) -> None:
    raw = _forwarded(kit, f"dmarc=pass header.from={DOMAIN}")
    tampered = mailkit.replace_header(raw, "Subject", "Changed after the seal")
    result = _via_forwarder(kit, tampered, frozenset({SEALER}))
    assert result.summary["arc"]["cv"] == "fail"
    assert result.sender_auth == "fail"


# ─── DNS failures, caps and the summary ──────────────────────────────────────────────────────────


def test_dmarc_lookup_failure_or_query_cap_fails(kit: mailkit.Kit) -> None:
    raw = kit.dkim(mailkit.message(MEMBER), DOMAIN)
    kit.zone.fail(f"_dmarc.{DOMAIN}")
    result = _verify(kit, raw)
    assert (result.sender_auth, result.summary["reason"]) == ("fail", "temperror")

    kit = mailkit.Kit()
    kit.dmarc(DOMAIN)
    raw = kit.dkim(mailkit.message(MEMBER), DOMAIN)
    result = _verify(kit, raw, mail_from="", peer="", max_queries=1)  # the key, then no room
    assert result.summary["dns"]["cap_hit"] is True
    assert (result.sender_auth, result.summary["reason"]) == ("fail", "temperror")


def test_a_failed_lookup_for_one_identifier_does_not_hide_another(kit: mailkit.Kit) -> None:
    kit.spf(f"bounce.{DOMAIN}", "ip4:192.0.2.0/24")
    kit.zone.fail(f"_dmarc.bounce.{DOMAIN}")  # SPF's org-domain walk cannot finish
    raw = kit.dkim(mailkit.message(MEMBER), DOMAIN)
    result = _verify(kit, raw, mail_from=f"x@bounce.{DOMAIN}")
    assert result.sender_auth == "dmarc_pass"
    assert result.summary["dmarc"]["dkim_aligned"] == [DOMAIN]
    assert result.summary["dmarc"]["spf_aligned"] is False
    # Without the DKIM pass, the failed lookup is a temperror, not a plain misalignment.
    result = _verify(kit, mailkit.message(MEMBER), mail_from=f"x@bounce.{DOMAIN}")
    assert (result.sender_auth, result.summary["reason"]) == ("fail", "temperror")


def test_summary_holds_domains_and_results_but_no_local_part(kit: mailkit.Kit) -> None:
    raw = kit.dkim(mailkit.message(MEMBER, subject="Salary review"), DOMAIN)
    result = _verify(kit, raw)
    assert result.sender_auth == "dmarc_pass"
    text = json.dumps(result.summary)
    assert "member" not in text and "Salary" not in text and "bounce" not in text
    assert set(result.summary) == {
        "verdict", "reason", "from_domain", "spf", "dkim", "dmarc", "arc", "dns",
    }
