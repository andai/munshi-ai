"""Signed test mail and a stub DNS zone for the sender-verification tests (and 07-08's fixtures).

Keys are generated when the tests run (RSA, through `cryptography`) and live only in memory: no key
material is committed. `Kit` holds one stub zone (`dns.StubResolver`) and the keys it has published.

* `message(...)` builds a plain CRLF message.
* `kit.dkim(raw, domain)` prepends a DKIM-Signature for `domain` and publishes its key. Pass
  `expires=` to set an `x=` tag, `length=True` for an `l=` tag, or `publish=False` to leave the key
  out of DNS.
* `kit.arc(raw, sealer, results)` adds the forwarder's `Authentication-Results` and seals an ARC set
  for `sealer` over the message as it is now.
* `kit.spf(domain, *ips)` and `kit.dmarc(domain, ...)` publish SPF and DMARC records.
* `append_to_body` and `replace_header` change a message after signing, as a forwarder or an attacker
  would.

ed25519 is not offered: dkimpy verifies it only with PyNaCl, which andai-intake does not install.
"""

from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass, field
from functools import lru_cache

import dkim
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from dkim.canonicalization import CanonicalizationPolicy
from dkim.crypto import parse_pem_private_key

from andai_intake.auth.dns import StubResolver

NOTETAKER = "notetaker@munshi.test"
SELECTOR = "sel"


@dataclass(frozen=True)
class Key:
    private_pem: bytes  # PKCS#1, as dkimpy reads it
    public_b64: str  # SubjectPublicKeyInfo DER, base64: the DNS `p=` value

    @property
    def record(self) -> str:
        return f"v=DKIM1; k=rsa; p={self.public_b64}"


@lru_cache(maxsize=None)
def rsa_key(label: str = "default", bits: int = 2048) -> Key:
    """A fresh RSA key per label, shared within one test run."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    der = private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return Key(private_pem=pem, public_b64=base64.b64encode(der).decode("ascii"))


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


@dataclass
class Kit:
    zone: StubResolver = field(default_factory=StubResolver)

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
