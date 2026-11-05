"""Signed test mail and the DNS records that let it pass (or fail) the sender check.

Used in two places:

* the sender-verification tests (`tests/mailkit.py` re-exports this module), with a `Kit` over an
  in-memory zone (`dns.StubResolver`) and keys that live only in memory;
* the e2e stack, as a command run inside the `fake-dns` container, with a `Kit` over the zone file
  that `fake_dns` serves (`FileZone`) and keys kept next to that file:

      python -m andai_intake.testing.sign --zone /tmp/fake-dns/zone.json --domain d1.example.test \\
          < invite.eml > signed.eml

  It publishes `v=spf1 -all` and a DMARC record (`--dmarc`, default `reject`) for the domain,
  DKIM-signs the message read from stdin with the domain's key (created on first use) and publishes
  that key, then writes the signed message to stdout. `--unsigned` publishes the records but leaves
  the message unsigned, which is what a forged From looks like.

Keys are RSA (through `cryptography`), generated when first needed and never committed: in memory
for the tests, in the zone file's `keys/` directory (mode 0600) for the e2e stack. ed25519 is not
offered: dkimpy verifies it only with PyNaCl, which andai-intake does not install.

* `message(...)` builds a plain CRLF message.
* `kit.dkim(raw, domain)` prepends a DKIM-Signature for `domain` and publishes its key. Pass
  `expires=` to set an `x=` tag, `length=True` for an `l=` tag, or `publish=False` to leave the key
  out of DNS.
* `kit.arc(raw, sealer, results)` adds the forwarder's `Authentication-Results` and seals an ARC set
  for `sealer` over the message as it is now.
* `kit.spf(domain, *ips)` and `kit.dmarc(domain, ...)` publish SPF and DMARC records.
* `append_to_body` and `replace_header` change a message after signing, as a forwarder or an
  attacker would.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Protocol

import dkim
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from dkim.canonicalization import CanonicalizationPolicy
from dkim.crypto import parse_pem_private_key

from andai_intake.auth.dns import RDTYPES, StubResolver, normalise
from andai_intake.replay import wire_bytes

NOTETAKER = "notetaker@munshi.test"
SELECTOR = "sel"


@dataclass(frozen=True)
class Key:
    private_pem: bytes  # PKCS#1, as dkimpy reads it
    public_b64: str  # SubjectPublicKeyInfo DER, base64: the DNS `p=` value

    @property
    def record(self) -> str:
        return f"v=DKIM1; k=rsa; p={self.public_b64}"


def _key_from_private(private: rsa.RSAPrivateKey) -> Key:
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    der = private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return Key(private_pem=pem, public_b64=base64.b64encode(der).decode("ascii"))


@cache
def rsa_key(label: str = "default", bits: int = 2048) -> Key:
    """A fresh RSA key per label, shared within one test run."""
    return _key_from_private(rsa.generate_private_key(public_exponent=65537, key_size=bits))


def stored_key(directory: Path, domain: str, bits: int = 2048) -> Key:
    """The key `domain` signs with, kept in `directory` (created on first use, mode 0600), so every
    message of one e2e run is signed with the key its DNS record publishes."""
    name = normalise(domain)
    if not name or "/" in name or name.startswith("."):
        raise ValueError(f"bad domain {domain!r}")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{name}.pem"
    with _locked(directory / ".lock"):
        if path.exists():
            private = serialization.load_pem_private_key(path.read_bytes(), password=None)
            assert isinstance(private, rsa.RSAPrivateKey)
            return _key_from_private(private)
        key = _key_from_private(rsa.generate_private_key(public_exponent=65537, key_size=bits))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(key.private_pem)
        return key


def message(
    from_addr: str = "member@a.example.test",
    *,
    to: str = NOTETAKER,
    subject: str = "Weekly sync",
    body: str = "Join the meeting.\r\n",
    headers: tuple[tuple[str, str], ...] = (),
    message_id: str | None = None,
) -> bytes:
    lines = [
        f"From: {from_addr}",
        f"To: {to}",
        f"Subject: {subject}",
        "Date: Thu, 05 Nov 2026 10:00:00 +0000",
        f"Message-ID: {message_id or '<' + hashlib.sha1(body.encode()).hexdigest() + '@test>'}",
        "MIME-Version: 1.0",
        "Content-Type: text/plain; charset=utf-8",
        *(f"{name}: {value}" for name, value in headers),
    ]
    return ("\r\n".join(lines) + "\r\n\r\n" + body).encode("utf-8")


def _split(raw: bytes) -> tuple[bytes, bytes]:
    head, sep, body = raw.partition(b"\r\n\r\n")
    return head + sep, body


def append_to_body(raw: bytes, text: str) -> bytes:
    head, body = _split(raw)
    return head + body + text.encode("utf-8")


def replace_header(raw: bytes, name: str, value: str) -> bytes:
    head, body = _split(raw)
    lines = head.split(b"\r\n")
    prefix = name.lower().encode("ascii") + b":"
    out = [
        f"{name}: {value}".encode() if line.lower().startswith(prefix) else line for line in lines
    ]
    return b"\r\n".join(out) + body


def _sign_with_expiry(raw: bytes, domain: str, selector: str, key: Key, expires: int) -> bytes:
    """dkimpy's sign() has no `x=`; build the same header with it through dkimpy's own signer."""
    signer = dkim.DKIM(raw)
    canon = CanonicalizationPolicy.from_c_value(b"relaxed/simple")
    include = tuple(h.lower() for h in signer.default_sign_headers())
    signer.hasher = dkim.HASH_ALGORITHMS[b"rsa-sha256"]
    body_hash = base64.b64encode(signer.hasher(canon.canonicalize_body(signer.body)).digest())
    signed_at = min(int(time.time()), expires - 1)
    fields = [
        (b"v", b"1"),
        (b"a", b"rsa-sha256"),
        (b"c", b"relaxed/simple"),
        (b"d", domain.encode()),
        (b"s", selector.encode()),
        (b"t", str(signed_at).encode()),
        (b"x", str(expires).encode()),
        (b"h", b" : ".join(include)),
        (b"bh", body_hash),
        (b"b", b"0" * 60),
    ]
    value = signer.gen_header(
        fields, include, canon, b"DKIM-Signature", parse_pem_private_key(key.private_pem)
    )
    return b"DKIM-Signature: " + value + raw


# ─── zones ───────────────────────────────────────────────────────────────────────────────────────


class Zone(Protocol):
    def txt(self, name: str, *values: str | bytes) -> Any: ...

    def remove(self, name: str, rdtype: str = "TXT") -> Any: ...


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    with open(path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_zone(path: Path) -> dict[str, dict[str, list[str]]]:
    """The zone file as `{name: {rdtype: [value, ...]}}`; empty when there is none yet. TXT values
    are whole records (`fake_dns` splits them into 255-byte strings), A values addresses."""
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    names = data.get("names") if isinstance(data, dict) else None
    if not isinstance(names, dict):
        raise TypeError(f"{path} is not a zone file")
    return names


class FileZone:
    """A zone kept in a JSON file that `fake_dns` re-reads on every query. Each change is written
    under a lock and swapped in whole, so a query never sees half a file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @contextlib.contextmanager
    def _change(self) -> Iterator[dict[str, dict[str, list[str]]]]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with _locked(self.path.with_name(self.path.name + ".lock")):
            names = read_zone(self.path)
            yield names
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".zone-")
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump({"names": names}, out, indent=1, sort_keys=True)
            os.replace(tmp, self.path)

    def add(self, name: str, rdtype: str, *values: str) -> FileZone:
        if rdtype not in RDTYPES:
            raise ValueError(f"unsupported record type {rdtype}")
        with self._change() as names:
            names.setdefault(normalise(name), {}).setdefault(rdtype, []).extend(
                v.decode("ascii") if isinstance(v, bytes) else v for v in values
            )
        return self

    def txt(self, name: str, *values: str | bytes) -> FileZone:
        return self.add(name, "TXT", *values)  # type: ignore[arg-type]

    def remove(self, name: str, rdtype: str = "TXT") -> FileZone:
        with self._change() as names:
            types = names.get(normalise(name), {})
            types.pop(rdtype, None)
            if not types:
                names.pop(normalise(name), None)
        return self

    def records(self, name: str, rdtype: str) -> list[str]:
        return list(read_zone(self.path).get(normalise(name), {}).get(rdtype, []))


@dataclass
class Kit:
    zone: Zone = field(default_factory=StubResolver)

    def publish_key(self, domain: str, key: Key, selector: str = SELECTOR) -> None:
        self.zone.remove(f"{selector}._domainkey.{domain}")
        self.zone.txt(f"{selector}._domainkey.{domain}", key.record)

    def dkim(
        self,
        raw: bytes,
        domain: str,
        *,
        selector: str = SELECTOR,
        key: Key | None = None,
        publish: bool = True,
        expires: int | None = None,
        length: bool = False,
    ) -> bytes:
        key = key or rsa_key(domain)
        if publish:
            self.publish_key(domain, key, selector)
        if expires is not None:
            return _sign_with_expiry(raw, domain, selector, key, expires)
        header = dkim.sign(
            raw, selector.encode(), domain.encode(), key.private_pem, length=length
        )
        return header + raw

    def arc(
        self,
        raw: bytes,
        sealer: str,
        results: str,
        *,
        selector: str = SELECTOR,
        key: Key | None = None,
    ) -> bytes:
        """Seal `raw` as forwarder `sealer` that recorded `results` (e.g. `dmarc=pass
        header.from=a.example.test`) in its own Authentication-Results."""
        key = key or rsa_key(sealer)
        self.publish_key(sealer, key, selector)
        stamped = f"Authentication-Results: {sealer}; {results}\r\n".encode() + raw
        arc_set = dkim.arc_sign(
            stamped, selector.encode(), sealer.encode(), key.private_pem, sealer.encode()
        )
        assert arc_set, "dkimpy produced no ARC set"
        return b"".join(arc_set) + stamped

    def spf(self, domain: str, *mechanisms: str, all_: str = "-all") -> None:
        self.zone.remove(domain)
        self.zone.txt(domain, " ".join(("v=spf1", *mechanisms, all_)))

    def dmarc(self, domain: str, *, p: str = "reject", **tags: str) -> None:
        self.zone.remove(f"_dmarc.{domain}")
        extra = "".join(f"; {k}={v}" for k, v in tags.items())
        self.zone.txt(f"_dmarc.{domain}", f"v=DMARC1; p={p}{extra}")


# ─── the e2e command ─────────────────────────────────────────────────────────────────────────────


def sign_for_e2e(
    raw: bytes,
    *,
    zone: Path,
    domain: str,
    selector: str = SELECTOR,
    dmarc: str = "reject",
    signed: bool = True,
    keys: Path | None = None,
) -> bytes:
    """Publish the domain's SPF (`-all`: only DKIM can align) and DMARC records in the zone file,
    and, unless `signed` is false, DKIM-sign `raw` with the domain's stored key and publish it."""
    kit = Kit(zone=FileZone(zone))
    kit.spf(domain)
    kit.dmarc(domain, p=dmarc)
    data = wire_bytes(raw)
    if not signed:
        return data
    key = stored_key(keys or Path(zone).parent / "keys", domain)
    return kit.dkim(data, domain, selector=selector, key=key)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m andai_intake.testing.sign",
        description="Sign a message read from stdin for the e2e stack's fake DNS; print it.",
    )
    parser.add_argument("--zone", required=True, type=Path, help="the zone file fake_dns serves")
    parser.add_argument("--domain", required=True, help="the From domain to publish and sign for")
    parser.add_argument("--selector", default=SELECTOR)
    parser.add_argument("--dmarc", default="reject", choices=("none", "quarantine", "reject"))
    parser.add_argument("--unsigned", action="store_true", help="publish records, do not sign")
    parser.add_argument("--keys", type=Path, default=None, help="key directory (zone dir/keys)")
    args = parser.parse_args(argv)

    raw = sys.stdin.buffer.read()
    if not raw.strip():
        parser.error("no message on stdin")
    out = sign_for_e2e(
        raw,
        zone=args.zone,
        domain=args.domain,
        selector=args.selector,
        dmarc=args.dmarc,
        signed=not args.unsigned,
        keys=args.keys,
    )
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
