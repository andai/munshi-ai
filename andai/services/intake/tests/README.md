# andai/services/intake/tests

andai-intake tests. `conftest.py` re-exports andai-core's fixtures (`services/core/tests/conftest.py`):
a disposable Postgres initialised with `db-init.sql` and migrated to head, and a disposable Valkey.
Without Docker those tests skip; `ANDAI_REQUIRE_DOCKER=1` makes that a failure.

- `test_smtp_receive.py` runs the real aiosmtpd receiver on an ephemeral loopback port, with stdlib
  smtplib as the sender. It covers recipient and size refusals, store-then-250, 451 on a store
  failure, dedup and replay.
- `test_purge.py` covers the 7/30-day retention and the store's 24-hour dedup.
- `mailkit.py` is not a test. It re-exports `andai_intake.testing.sign`, which generates RSA keys at
  test time (never committed), signs mail with DKIM, seals ARC sets as a forwarder would, and
  publishes keys, SPF and DMARC records in a stub DNS zone.
- `test_fake_dns.py` covers the e2e stack's stub DNS server: TXT and A answers over UDP and TCP,
  NXDOMAIN outside the zone, truncation and the TCP retry for a large record, and a record the e2e
  signer writes answered on the next query, up to a signed message passing the sender check.
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

- `test_events.py` covers the invite state machine without a database: create, update by SEQUENCE
  and DTSTAMP, stale and duplicate messages, out-of-order arrival, series and instance cancels,
  instance-only invites, overrides, skips kept through updates, the owner rule, joined-now and
  ended invites, and the top-up.
- `schedulekit.py` is not a test. It builds an org whose domain signs its mail in a stub DNS zone,
  signed invite mail from its members, delivery into `inbound_mail`, and runs the processor until a
  message has its result.
- `test_processor.py` covers the processor end to end: scheduled meetings, every quarantine and
  ignore reason, `bot.autoJoin` with team overrides, joined-now, the lease after a crash, the
  5-attempt cap, the 30-day purge next to invite events, and org-scoped writes.
- `test_horizon.py` covers the hourly top-up: a weekly series' third occurrence, EXDATE, a cancelled
  series, `bot.autoJoin` off, and skips.
- `test_confirm.py` covers the confirmations through Mailpit: one per outcome with title, time in
  the invite's zone and UTC, platform and reason, the series count, the forwarded-copy note, none for
  unverified, unknown, automatic, list or bulk mail or a null reverse-path, the RFC 3834 guard, 30
  per recipient per hour, a header injection in SUMMARY, a relay failure, and nothing before commit.

The sender-verification tests block dnspython's default resolver, so they cannot reach the network.

Run from the repo root:
`ANDAI_REQUIRE_DOCKER=1 uv run --directory andai pytest -q services/intake/tests`.
