# andai/services/intake/tests

andai-intake tests. `conftest.py` re-exports andai-core's fixtures (`services/core/tests/conftest.py`):
a disposable Postgres initialised with `db-init.sql` and migrated to head, and a disposable Valkey.
Without Docker those tests skip; `ANDAI_REQUIRE_DOCKER=1` makes that a failure.

- `test_smtp_receive.py` runs the real aiosmtpd receiver on an ephemeral loopback port, with stdlib
  smtplib as the sender. It covers recipient and size refusals, store-then-250, 451 on a store
  failure, dedup and replay.
- `test_purge.py` covers the 7/30-day retention and the store's 24-hour dedup.
- `mailkit.py` is not a test. It generates RSA keys at test time (never committed), signs mail with
  DKIM, seals ARC sets as a forwarder would, and publishes keys, SPF and DMARC records in a stub DNS
  zone.
- `test_auth_spf_dkim.py` covers SPF and DKIM results on stub DNS, the resolver's cache and timeout,
  and the per-message query cap and deadline.
- `test_auth_dmarc_arc.py` covers DMARC alignment by tree walk, the ARC trust rule, and the verdict,
  including the spoofed sender (`sender_unverified`).
- `test_sender.py` covers sender resolution and quarantine attribution through the 0013 functions.
- `test_limits.py` covers the per-sender, per-org and per-peer limits, on Valkey and in memory.
- `invitekit.py` is not a test. It builds synthetic VEVENTs, VCALENDARs and invite mail.
- `test_invite_mime.py` covers calendar extraction (inline, attached, one level of forwarded
  message), deduplication, and the part, depth, size, VEVENT and time caps.
- `test_invite_ics.py` covers METHOD, UID normalisation, RECURRENCE-ID, SEQUENCE, DTSTAMP,
  ORGANIZER/ATTENDEE, SUMMARY and the forward flags.
- `test_invite_tz_recur.py` covers Windows TZIDs and DST, custom VTIMEZONEs (no sharing between
  messages), floating and all-day times, and recurrence with EXDATE, RDATE, overrides, COUNT/UNTIL
  and the caps. It reads the BSD-2 calendars in `calendars/` (see the README there).
- `test_invite_links.py` covers where join links are found, Safe Links, Proofpoint v1/v2/v3 and
  Google unwrapping, Mimecast, and refused links.

The sender-verification tests block dnspython's default resolver, so they cannot reach the network.

Run from the repo root:
`ANDAI_REQUIRE_DOCKER=1 uv run --directory andai pytest -q services/intake/tests`.
