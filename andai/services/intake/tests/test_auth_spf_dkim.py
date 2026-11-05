"""SPF and DKIM on stub DNS, the resolver's cache, timeout and per-message caps. No network: the
library default resolvers are blocked for every test here."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import dns.exception
import dns.resolver
import pytest

import mailkit
from andai_intake.auth import dkim, spf
from andai_intake.auth.dns import (
    DeadlineExceeded,
    DnsResolver,
    DnsTempError,
    QueryCapExceeded,
    Session,
    StubResolver,
    parse_nameservers,
    run_capped,
)

DOMAIN = "a.example.test"
PEER = "192.0.2.25"


@pytest.fixture(autouse=True)
def no_system_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError("a library fell back to the system resolver")

    monkeypatch.setattr(dns.resolver, "resolve", refuse)
    monkeypatch.setattr(dns.resolver, "query", refuse)


@pytest.fixture
def kit() -> mailkit.Kit:
    return mailkit.Kit()


def _spf(kit: mailkit.Kit, ip: str = PEER, mail_from: str = f"bounce@{DOMAIN}", **session) -> str:
    return spf.check(Session(kit.zone, **session), ip, mail_from, f"mx.{DOMAIN}").result


# ─── SPF ──────────────────────────────────────────────────────────────────────────────────────────


def test_spf_results_for_the_mail_from_domain(kit: mailkit.Kit) -> None:
    kit.spf(DOMAIN, "ip4:192.0.2.0/24")
    assert _spf(kit) == "pass"
    assert _spf(kit, ip="198.51.100.7") == "fail"
    kit.spf(DOMAIN, "ip4:192.0.2.0/24", all_="~all")
    assert _spf(kit, ip="198.51.100.7") == "softfail"
    kit.spf(DOMAIN, "ip4:192.0.2.0/24", all_="?all")
    assert _spf(kit, ip="198.51.100.7") == "neutral"
    kit.spf(DOMAIN, "ip4:192.0.2.0/24", "moo")
    assert _spf(kit) == "permerror"
    assert _spf(kit, mail_from="bounce@no-record.example.test") == "none"


def test_spf_follows_include_and_mx(kit: mailkit.Kit) -> None:
    kit.spf(DOMAIN, "include:_spf.mail.example.test", "mx")
    kit.spf("_spf.mail.example.test", "ip4:203.0.113.0/24")
    kit.zone.add(DOMAIN, "MX", (10, f"mx.{DOMAIN}"))
    kit.zone.add(f"mx.{DOMAIN}", "A", "198.51.100.9")
    assert _spf(kit, ip="203.0.113.4") == "pass"
    assert _spf(kit, ip="198.51.100.9") == "pass"
    assert _spf(kit, ip="198.51.100.10") == "fail"


def test_spf_null_mail_from_checks_the_helo(kit: mailkit.Kit) -> None:
    kit.spf(f"mx.{DOMAIN}", "ip4:192.0.2.25")
    result = spf.check(Session(kit.zone), PEER, "", f"mx.{DOMAIN}")
    assert (result.result, result.domain, result.scope) == ("pass", f"mx.{DOMAIN}", "helo")


def test_spf_ten_lookup_limit_is_a_permerror(kit: mailkit.Kit) -> None:
    kit.spf(DOMAIN, *(f"include:i{n}.example.test" for n in range(11)))
    for n in range(11):
        kit.spf(f"i{n}.example.test", "ip4:203.0.113.1")
    assert _spf(kit, ip="198.51.100.1") == "permerror"


def test_spf_void_lookup_limit_is_a_permerror(kit: mailkit.Kit) -> None:
    kit.spf(DOMAIN, "a:void1.example.test", "a:void2.example.test", "a:void3.example.test")
    assert _spf(kit, ip="198.51.100.1") == "permerror"
    kit.spf(DOMAIN, "a:void1.example.test", "a:void2.example.test")
    assert _spf(kit, ip="198.51.100.1") == "fail"  # two void lookups are allowed


def test_spf_dns_failure_and_query_cap_are_temperror(kit: mailkit.Kit) -> None:
    kit.spf(DOMAIN, "include:_spf.mail.example.test")
    kit.spf("_spf.mail.example.test", "ip4:192.0.2.0/24")
    kit.zone.fail("_spf.mail.example.test")
    assert _spf(kit) == "temperror"

    kit = mailkit.Kit()
    kit.spf(DOMAIN, "include:_spf.mail.example.test")
    kit.spf("_spf.mail.example.test", "ip4:192.0.2.0/24")
    session = Session(kit.zone, max_queries=1)
    assert spf.check(session, PEER, f"bounce@{DOMAIN}", None).result == "temperror"
    assert session.cap_hit and session.queries == 1


def test_spf_without_a_peer_is_none_and_asks_nothing(kit: mailkit.Kit) -> None:
    kit.spf(DOMAIN, "ip4:192.0.2.0/24")
    assert spf.check(Session(kit.zone), None, f"bounce@{DOMAIN}", None).result == "none"
    assert kit.zone.queries == []


def test_spf_outside_a_session_never_uses_the_system_resolver() -> None:
    with pytest.raises(spf.pyspf.TempError):
        spf._lookup("example.test", "TXT")


# ─── DKIM ─────────────────────────────────────────────────────────────────────────────────────────


def _dkim(kit: mailkit.Kit, raw: bytes, **session) -> list[tuple[str | None, str, str | None]]:
    return [(r.domain, r.result, r.reason) for r in dkim.check(Session(kit.zone, **session), raw)]


def test_dkim_pass_on_the_raw_bytes(kit: mailkit.Kit) -> None:
    raw = kit.dkim(mailkit.message(), DOMAIN)
    assert _dkim(kit, raw) == [(DOMAIN, "pass", None)]
    # LF line endings (as some captures arrive) verify the same.
    assert _dkim(kit, raw.replace(b"\r\n", b"\n")) == [(DOMAIN, "pass", None)]


def test_dkim_body_or_header_change_fails(kit: mailkit.Kit) -> None:
    raw = kit.dkim(mailkit.message(), DOMAIN)
    assert _dkim(kit, mailkit.append_to_body(raw, "-- \r\nfooter\r\n")) == [
        (DOMAIN, "fail", "body_hash_mismatch")
    ]
    assert _dkim(kit, mailkit.replace_header(raw, "Subject", "Moved")) == [
        (DOMAIN, "fail", "signature_mismatch")
    ]
    assert _dkim(kit, mailkit.replace_header(raw, "From", "boss@a.example.test")) == [
        (DOMAIN, "fail", "signature_mismatch")
    ]


def test_dkim_missing_key_and_expired_signature_never_pass(kit: mailkit.Kit) -> None:
    unpublished = kit.dkim(mailkit.message(), "b.example.test", publish=False)
    assert _dkim(kit, unpublished) == [("b.example.test", "permerror", "no_key")]
    expired = kit.dkim(mailkit.message(), DOMAIN, expires=int(time.time()) - 2 * 86400)
    assert _dkim(kit, expired) == [(DOMAIN, "permerror", "expired")]
    valid_until = kit.dkim(mailkit.message(), DOMAIN, expires=int(time.time()) + 86400)
    assert _dkim(kit, valid_until) == [(DOMAIN, "pass", None)]


def test_dkim_with_a_body_length_limit_never_passes(kit: mailkit.Kit) -> None:
    raw = kit.dkim(mailkit.message(), DOMAIN, length=True)
    assert _dkim(kit, raw) == [(DOMAIN, "permerror", "body_length_limit")]
    appended = mailkit.append_to_body(raw, "Join instead: https://evil.example.test/j/1\r\n")
    assert _dkim(kit, appended) == [(DOMAIN, "permerror", "body_length_limit")]


def test_dkim_wrong_key_fails(kit: mailkit.Kit) -> None:
    raw = kit.dkim(mailkit.message(), DOMAIN, key=mailkit.rsa_key("other"), publish=False)
    kit.publish_key(DOMAIN, mailkit.rsa_key(DOMAIN))
    assert _dkim(kit, raw) == [(DOMAIN, "fail", "signature_mismatch")]


def test_dkim_every_signature_is_reported(kit: mailkit.Kit) -> None:
    raw = kit.dkim(mailkit.message(), DOMAIN)
    raw = kit.dkim(raw, "esp.example.net")
    raw = kit.dkim(raw, "ghost.example.net", publish=False)
    assert _dkim(kit, raw) == [
        ("ghost.example.net", "permerror", "no_key"),
        ("esp.example.net", "pass", None),
        (DOMAIN, "pass", None),
    ]
    assert _dkim(kit, mailkit.message()) == []


def test_dkim_dns_failure_and_query_cap_are_temperror(kit: mailkit.Kit) -> None:
    raw = kit.dkim(kit.dkim(mailkit.message(), DOMAIN), "esp.example.net")
    assert _dkim(kit, raw, max_queries=1) == [
        ("esp.example.net", "pass", None),
        (DOMAIN, "temperror", "dns"),
    ]
    kit.zone.fail(f"sel._domainkey.{DOMAIN}")
    assert _dkim(kit, raw)[1] == (DOMAIN, "temperror", "dns")


# ─── the resolver, the session and the caps ──────────────────────────────────────────────────────


def test_parse_nameservers() -> None:
    assert parse_nameservers("") == ()
    assert parse_nameservers("10.0.0.2, 10.0.0.3:5353,[2001:db8::1]:53,2001:db8::2") == (
        ("10.0.0.2", 53),
        ("10.0.0.3", 5353),
        ("2001:db8::1", 53),
        ("2001:db8::2", 53),
    )
    for bad in ("dns.example.test", "10.0.0.2:0", "[2001:db8::1"):
        with pytest.raises(ValueError):
            parse_nameservers(bad)


def test_session_caps_queries_and_memoises_repeats() -> None:
    zone = StubResolver().txt("a.example.test", "x").txt("b.example.test", "y")
    session = Session(zone, max_queries=2)
    assert session.txt("a.example.test") == [b"x"]
    assert session.txt("A.Example.Test.") == [b"x"]  # memoised, not asked again
    assert session.txt("b.example.test") == [b"y"]
    with pytest.raises(QueryCapExceeded):
        session.txt("c.example.test")
    assert zone.queries == [("a.example.test", "TXT"), ("b.example.test", "TXT")]
    assert session.summary() == {
        "queries": 2, "temperror": True, "cap_hit": True, "deadline_hit": False,
    }


def test_session_deadline_cuts_a_slow_check() -> None:
    zone = StubResolver().txt("slow.example.test", "v=spf1 -all").slow("slow.example.test", 2.0)
    session = Session(zone, deadline_s=0.3)
    started = time.monotonic()
    result = spf.check(session, PEER, "bounce@slow.example.test", None)
    assert result.result == "temperror"
    assert time.monotonic() - started < 1.0
    assert session.deadline_hit
    with pytest.raises(DeadlineExceeded):
        session.txt("other.example.test")
    with pytest.raises(DeadlineExceeded):
        run_capped(lambda: 1, session)


class _FakeAnswer:
    def __init__(self, rrset) -> None:
        self.rrset = rrset


class _FakeTxt:
    def __init__(self, *strings: bytes) -> None:
        self.strings = strings


class _FakeRrset(list):
    ttl = 120


def test_resolver_caches_answers_and_misses_for_their_ttl() -> None:
    now = [1000.0]
    calls: list[tuple[str, str]] = []
    resolver = DnsResolver((("127.0.0.1", 53),), timeout_s=1, clock=lambda: now[0])

    def resolve(name, rdtype, **kwargs):
        calls.append((name, rdtype))
        if name.startswith("nx."):
            raise dns.resolver.NXDOMAIN()
        if name.startswith("down."):
            raise dns.exception.Timeout()
        return _FakeAnswer(_FakeRrset([_FakeTxt(b"v=spf1 ", b"-all")]))

    resolver._resolver.resolve = resolve
    assert resolver.lookup("A.example.test.", "TXT") == ((b"v=spf1 ", b"-all"),)
    assert resolver.lookup("a.example.test", "TXT") == ((b"v=spf1 ", b"-all"),)
    assert resolver.lookup("nx.example.test", "TXT") == ()
    assert resolver.lookup("nx.example.test", "TXT") == ()
    assert calls == [("a.example.test.", "TXT"), ("nx.example.test.", "TXT")]
    now[0] += 121  # the answer's TTL (120 s) and the 60 s miss TTL have passed
    resolver.lookup("a.example.test", "TXT")
    resolver.lookup("nx.example.test", "TXT")
    assert len(calls) == 4
    with pytest.raises(DnsTempError):
        resolver.lookup("down.example.test", "TXT")
    with pytest.raises(DnsTempError):  # a failure is not cached as an empty answer
        resolver.lookup("down.example.test", "TXT")
    assert len(calls) == 6


@pytest.fixture
def silent_dns_server() -> Iterator[int]:
    """A UDP port on loopback that reads queries and never answers."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.2)
    stop = threading.Event()

    def drain() -> None:
        while not stop.is_set():
            try:
                sock.recvfrom(4096)
            except OSError:
                continue

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    try:
        yield sock.getsockname()[1]
    finally:
        stop.set()
        thread.join(1)
        sock.close()


def test_resolver_timeout_is_honoured(silent_dns_server: int) -> None:
    resolver = DnsResolver((("127.0.0.1", silent_dns_server),), timeout_s=0.5)
    started = time.monotonic()
    with pytest.raises(DnsTempError, match="Timeout|LifetimeTimeout"):
        resolver.lookup("sel._domainkey.example.test", "TXT")
    assert 0.4 <= time.monotonic() - started < 2.0
