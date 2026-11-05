# andai/services/intake/andai_intake/auth

Sender verification for the notetaker mailbox (D-56). The checks run on the raw bytes exactly as
stored. Their DNS lookups are andai-intake's only egress.

- `dns.py` — `DnsResolver` (dnspython, `ANDAI_INTAKE_DNS_RESOLVER` and `ANDAI_INTAKE_DNS_TIMEOUT_S`,
  TTL cache), `StubResolver` for tests, and the per-message `Session`, which enforces
  `ANDAI_INTAKE_DNS_MAX_QUERIES` and a 20-second deadline.
- `spf.py` — SPF through pyspf, with its DNS hook pointed at the session.
- `dkim.py` — every DKIM signature through dkimpy.
- `dmarc.py` — RFC 9989 tree walk (policy, organizational domain) and alignment.
- `arc.py` — the ARC chain and the allowlisted-sealer rule (`ANDAI_INTAKE_ARC_SEALERS`).
- `verdict.py` — `verify(raw, mail_from, peer_ip, helo)` → `dmarc_pass | arc_trusted | fail` and the
  check summary stored on the `inbound_mail` row.
