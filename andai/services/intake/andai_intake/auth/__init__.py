"""Sender verification for the notetaker mailbox (D-56).

andai-intake decides itself whether a message comes from the domain its From header names. It runs
SPF, DKIM and ARC through dkimpy and pyspf on the raw bytes exactly as stored, and its own DMARC
alignment with the RFC 9989 DNS tree walk. Inbound `Authentication-Results` and vendor headers are
never trusted; an ARC hop counts only when its sealer is allowlisted.

* `dns.py` — the resolver from config (`ANDAI_INTAKE_DNS_*`): the intake's only egress. It has a
  per-lookup timeout, a TTL cache, and a per-message session with a query cap and a deadline.
  `StubResolver` serves a fixed zone to tests, so they make no network calls.
* `spf.py` — SPF (RFC 7208) of the peer address for the MAIL FROM domain (or the HELO for a null
  MAIL FROM), through pyspf.
* `dkim.py` — every DKIM signature (RFC 6376, at most 8), through dkimpy.
* `dmarc.py` — RFC 9989 policy discovery and organizational domain by DNS tree walk, and
  relaxed/strict alignment.
* `arc.py` — the ARC chain (RFC 8617) and the trust rule for an allowlisted sealer.
* `verdict.py` — `verify(raw, mail_from, peer_ip, helo)` returns the verdict (`dmarc_pass`,
  `arc_trusted` or `fail`) and the check summary stored on the `inbound_mail` row.
"""
