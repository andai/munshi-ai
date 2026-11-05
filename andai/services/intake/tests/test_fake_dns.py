"""The e2e stack's stub DNS server (`andai_intake.testing.fake_dns`) and the signer that writes its
zone (`andai_intake.testing.sign`): TXT and A answers over UDP and TCP, NXDOMAIN for anything not
in the zone, truncation and the TCP retry for a large record, and a record the signer writes being
answered on the next query, to the point where the signed message passes the sender check."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import dns.flags
import dns.message
import dns.query
import dns.rcode
import pytest
from andai_intake.auth.dns import DnsResolver
from andai_intake.auth.verdict import Policy, verify
from andai_intake.testing import fake_dns, sign
from andai_intake.testing.sign import FileZone

DOMAIN = "d1.example.test"
SENDER = f"ana@{DOMAIN}"


@pytest.fixture
def zone(tmp_path: Path) -> Path:
    path = tmp_path / "fake-dns" / "zone.json"
    FileZone(path).txt("txt.example.test", "v=spf1 -all").add("a.example.test", "A", "192.0.2.7")
    return path


def _ask(port: int, name: str, rdtype: str, *, tcp: bool, edns: int = -1) -> dns.message.Message:
    query = dns.message.make_query(name, rdtype, use_edns=edns)
    send = dns.query.tcp if tcp else dns.query.udp
    return send(query, "127.0.0.1", port=port, timeout=3)


def _texts(response: dns.message.Message) -> list[bytes]:
    return [b"".join(r.strings) for rrset in response.answer for r in rrset]


@pytest.mark.parametrize("tcp", [False, True], ids=["udp", "tcp"])
def test_serves_txt_and_a_records_from_the_zone(zone: Path, tcp: bool) -> None:
    with fake_dns.running(zone) as port:
        txt = _ask(port, "TXT.example.test.", "TXT", tcp=tcp)
        a = _ask(port, "a.example.test", "A", tcp=tcp)
    assert txt.rcode() == dns.rcode.NOERROR
    assert _texts(txt) == [b"v=spf1 -all"]
    assert txt.answer[0].ttl == fake_dns.TTL
    assert a.rcode() == dns.rcode.NOERROR
    assert [r.address for rrset in a.answer for r in rrset] == ["192.0.2.7"]


@pytest.mark.parametrize("tcp", [False, True], ids=["udp", "tcp"])
def test_nxdomain_outside_the_zone_and_empty_for_another_type(zone: Path, tcp: bool) -> None:
    with fake_dns.running(zone) as port:
        missing = _ask(port, "nothing.example.test", "TXT", tcp=tcp)
        other_type = _ask(port, "txt.example.test", "A", tcp=tcp)
        mx = _ask(port, "txt.example.test", "MX", tcp=tcp)
    assert missing.rcode() == dns.rcode.NXDOMAIN
    assert missing.answer == []
    assert other_type.rcode() == dns.rcode.NOERROR
    assert other_type.answer == []
    assert mx.rcode() == dns.rcode.NOERROR
    assert mx.answer == []


def test_no_zone_file_yet_is_nxdomain(tmp_path: Path) -> None:
    with fake_dns.running(tmp_path / "zone.json") as port:
        assert _ask(port, "txt.example.test", "TXT", tcp=False).rcode() == dns.rcode.NXDOMAIN


def test_a_large_record_is_truncated_over_udp_and_whole_over_tcp(zone: Path) -> None:
    key = sign.rsa_key("large", bits=4096)
    FileZone(zone).txt(f"sel._domainkey.{DOMAIN}", key.record)
    name = f"sel._domainkey.{DOMAIN}"
    with fake_dns.running(zone) as port:
        udp = _ask(port, name, "TXT", tcp=False)
        tcp = _ask(port, name, "TXT", tcp=True)
        # The intake's resolver (dnspython) repeats a truncated question over TCP by itself.
        resolved = DnsResolver((("127.0.0.1", port),), timeout_s=3).lookup(name, "TXT")
    assert udp.flags & dns.flags.TC and udp.answer == []
    assert _texts(tcp) == [key.record.encode()]
    assert [b"".join(strings) for strings in resolved] == [key.record.encode()]
    assert all(len(s) <= 255 for strings in resolved for s in strings)


def test_ignores_what_is_not_dns(zone: Path) -> None:
    import socket

    with fake_dns.running(zone) as port:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as raw:
            raw.settimeout(0.5)
            raw.sendto(b"not dns", ("127.0.0.1", port))
            with pytest.raises(TimeoutError):
                raw.recv(512)
        assert fake_dns.probe("127.0.0.1", port)


def test_probe_fails_without_a_server(tmp_path: Path) -> None:
    with fake_dns.running(tmp_path / "zone.json") as port:
        pass
    assert not fake_dns.probe("127.0.0.1", port, timeout=0.3)


def test_a_record_written_by_the_signer_is_answered_on_the_next_query(
    zone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = sign.message(SENDER, subject="Design review", body="Join here.\r\n")
    with fake_dns.running(zone) as port:
        before = _ask(port, f"sel._domainkey.{DOMAIN}", "TXT", tcp=False)

        out = io.BytesIO()
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(out))
        assert sign.main(["--zone", str(zone), "--domain", DOMAIN]) == 0
        sys.stdout.flush()
        signed = out.getvalue()

        key_record = _ask(port, f"sel._domainkey.{DOMAIN}", "TXT", tcp=True)
        dmarc = _ask(port, f"_dmarc.{DOMAIN}", "TXT", tcp=False)
        spf = _ask(port, DOMAIN, "TXT", tcp=False)

        # The whole sender check, through the fake server, passes for the signed message …
        policy = Policy(resolver=DnsResolver((("127.0.0.1", port),), timeout_s=3))
        passed = verify(signed, SENDER, "127.0.0.1", "mx.example.test", policy)
        # … and fails for the same message sent unsigned (a forged From).
        unsigned = sign.sign_for_e2e(raw, zone=zone, domain=DOMAIN, signed=False)
        forged = verify(unsigned, SENDER, "127.0.0.1", "mx.example.test", policy)

    assert before.rcode() == dns.rcode.NXDOMAIN
    assert signed.startswith(b"DKIM-Signature:")
    assert _texts(key_record)[0].startswith(b"v=DKIM1; k=rsa; p=")
    assert _texts(dmarc) == [b"v=DMARC1; p=reject"]
    assert _texts(spf) == [b"v=spf1 -all"]
    assert passed.sender_auth == "dmarc_pass"
    assert forged.sender_auth == "fail"
    assert b"DKIM-Signature" not in unsigned


def test_the_signer_keeps_one_key_per_domain(tmp_path: Path) -> None:
    zone = tmp_path / "zone.json"
    first = sign.sign_for_e2e(sign.message(SENDER), zone=zone, domain=DOMAIN)
    record = FileZone(zone).records(f"sel._domainkey.{DOMAIN}", "TXT")
    second = sign.sign_for_e2e(sign.message(SENDER, body="Again.\r\n"), zone=zone, domain=DOMAIN)
    assert FileZone(zone).records(f"sel._domainkey.{DOMAIN}", "TXT") == record
    assert len(record) == 1
    assert first != second
    pem = tmp_path / "keys" / f"{DOMAIN}.pem"
    assert pem.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        sign.stored_key(tmp_path / "keys", "../escape")
