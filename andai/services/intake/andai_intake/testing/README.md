# andai/services/intake/andai_intake/testing

Test tooling, never used by the service itself.

- `sign.py` — signed synthetic mail and the DNS records that make it pass or fail the sender check:
  the `Kit` the tests use (over an in-memory zone; `tests/mailkit.py` re-exports it), and
  `python -m andai_intake.testing.sign`, which the e2e suite runs inside the `fake-dns` container to
  publish a domain's SPF, DMARC and DKIM key in the zone file and sign a message read from stdin
  (`--unsigned` for a forged From).
- `fake_dns.py` — `python -m andai_intake.testing.fake_dns`: a stub DNS server answering TXT and A
  questions from that zone file over UDP and TCP (NXDOMAIN otherwise), re-read on every query. It
  runs only as the `fake-dns` service of the `e2e` compose profile, in andai-intake's network
  namespace on 127.0.0.1:5353.
