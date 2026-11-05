# andai/services/intake/andai_intake

Python package of the andai-intake service.

- `main.py` — the FastAPI app (`/healthz`); its lifespan starts the SMTP receiver, the processor,
  the horizon top-up and the purge loop when `ANDAI_DB_URL_APP` is set.
- `settings.py` — andai-core's settings plus `ANDAI_INTAKE_*` (addresses, bind, size cap, retention,
  DNS resolver and caps, ARC sealers, rate limits, scheduling horizon, join lead).
- `processor.py` — every stored message through the limits, the sender check, the sender, parsing,
  `bot.autoJoin` and the state machine, its result recorded with `andai_finish_inbound` in the same
  transaction as its meetings (see below); the confirmation is decided there and sent after the
  commit.
- `confirm.py` — the plain-text confirmation to the verified sender (scheduled, updated, cancelled,
  joining now, ignored or could not schedule, with the time in the invite's zone and UTC, and the
  forwarded-copy note), the RFC 3834 guard and 30 per recipient per hour.
- `events.py` — the invite state machine, pure: one message against one (org, UID) group → event
  rows, meetings to create, move or cancel, and the outcome.
- `horizon.py` — the hourly top-up: occurrences of active recurring invites that come within the
  horizon.
- `smtp.py` — the aiosmtpd handler: recipient check (550), recipient cap (452), size cap (552), store
  then 250, 451 on any store failure.
- `store.py` — `save()`: one `inbound_mail` row through `andai_store_inbound`, deduplicated on the
  raw bytes within 24 hours.
- `purge.py` — raw bytes nulled after 7 days, rows deleted after 30 (`andai_purge_inbound`).
- `replay.py` — `python -m andai_intake.replay`: send captured `.eml` files over SMTP.
- `auth/` — sender verification: SPF, DKIM, ARC and DMARC alignment → `dmarc_pass`, `arc_trusted`
  or `fail` (see below).
- `sender.py` — `resolve()`: the member a From address belongs to, and `attribute_org()`: whose
  admins see a quarantined message (migration 0013 functions).
- `limits.py` — per-sender, per-org and per-peer-IP hourly limits in Valkey.
- `invite/` — invite parsing: MIME, ICS fields, time zones, recurrence, join links and the forward
  flags, offline and bounded (see `invite/README.md`).

## Who may schedule the notetaker

A message counts only when its sender passes the check **and** is an active member of an org.
Everything else is quarantined with a reason, and the check summary is kept for 30 days.

**The check (D-56).** andai-intake verifies the sender itself. It runs SPF, DKIM and ARC on the
stored bytes and its own DMARC alignment (RFC 9989 DNS tree walk, no public-suffix list).
`Authentication-Results`, `X-MS-Exchange-*` and `X-Google-*` headers in the mail are never trusted.

- `dmarc_pass` — the From domain publishes a DMARC record (any policy, `p=none` included), and SPF
  for the MAIL FROM or a DKIM signature passes and aligns with the From domain (relaxed unless the
  record says `adkim=s` / `aspf=s`).
- `arc_trusted` — DMARC failed because a forwarder changed the mail, but the ARC chain is valid,
  its newest seal comes from a domain in `ANDAI_INTAKE_ARC_SEALERS`, and that hop recorded
  `dmarc=pass` for the From domain. The list ships empty; adding a sealer needs the CTO.
- `fail` — everything else, quarantined `sender_unverified`. This covers no alignment, no DMARC
  record for the From domain, a From header that does not name exactly one address, and a DNS
  failure, timeout or query cap (`ANDAI_INTAKE_DNS_MAX_QUERIES`). A DKIM signature with an `l=`
  body-length limit never counts, because content appended after the signed part would ride on it.
  An ed25519 signature never counts either, because PyNaCl is not installed; Google and Microsoft 365
  sign with RSA.

**What a customer's mail admin must set up.** An invite passes only when the sending domain signs
with its own DKIM key (or its SPF aligns) and publishes a DMARC record:

- **Google Workspace**: Calendar invites come from `calendar-server.bounces.google.com` (SPF never
  aligns) and are always signed `d=google.com`. They pass only when the admin turns on DKIM for the
  domain (Admin console → Apps → Google Workspace → Gmail → Authenticate email) and publishes
  `_dmarc.<domain>`. Without that, every Google invite is quarantined; there is no lower trust tier.
- **Microsoft 365**: without custom-domain DKIM, Exchange Online signs `d=<tenant>.onmicrosoft.com`,
  which does not align. DMARC then rests on SPF alone, which breaks as soon as the invite is
  forwarded. The admin enables DKIM for the custom domain (Defender portal → Email authentication
  settings → DKIM) and publishes `_dmarc.<domain>`.

**The sender (D-58).** A verified From address must belong to an active user (not disabled, not
only invited) with a membership in exactly one org. Their team is used when they are in exactly one
team. Addresses match as stored (lower-case). `ana+notes@x` also matches a stored `ana@x`, but never
a stored `ana+work@x`. Anyone else is quarantined `unknown_user`. A member of several orgs is
`unknown_user` with no org, because the mail does not say which org it is for.

**Who sees a quarantined message.** The admins of the org that the claimed From address is an
active member of, when there is exactly one. That holds for a forged From too. Otherwise it is the
one org whose allowed email domains contain the address's domain. When none or several match, or
the address is a member of several orgs, only the operator sees it.

**Limits (D-58).** Per sliding hour: 20 verified invites per sender and 200 per org
(`ANDAI_INTAKE_RATE_SENDER_PER_H`, `ANDAI_INTAKE_RATE_ORG_PER_H`), and 120 messages of any kind per
delivering IP (`ANDAI_INTAKE_RATE_PEER_PER_H`). Only verified mail counts against a sender or an
org, so a forged From cannot use up a member's quota. Excess mail is quarantined `rate_limited`.

**DNS.** All lookups go to `ANDAI_INTAKE_DNS_RESOLVER`, or the container's resolver when that is
empty. Each lookup is capped at `ANDAI_INTAKE_DNS_TIMEOUT_S` (3 s), and one message may make at
most `ANDAI_INTAKE_DNS_MAX_QUERIES` (30) lookups within 20 seconds.

## From an invite to a scheduled meeting

**Outcomes.** Every message ends with one SPEC `InboundInvite` outcome: `scheduled`, `updated`,
`cancelled` or `joined_now` (status `done`), `quarantined` with a reason (`sender_unverified`,
`unknown_user`, `rate_limited`, `parse_error`, `no_meeting_link`), or `ignored` with a reason:
`method_not_handled` (REPLY, COUNTER, REFRESH, ADD, PUBLISH or no METHOD), `not_event_owner`,
`stale`, `duplicate`, `ended`, `all_day`, `unsupported_recurrence`, `auto_join_off` or
`auto_forwarded`. A message that keeps failing is quarantined `parse_error` after 5 attempts.

**Events.** Invites are tracked per org by (normalised UID, RECURRENCE-ID) in `invite_events`, in RFC
5546 order: a higher SEQUENCE wins, then a later DTSTAMP; an older or identical VEVENT changes
nothing. REQUEST creates or updates; CANCEL without RECURRENCE-ID cancels the whole invite, with one
cancels that occurrence. Once an invite is tracked, only its requester (the member whose mail
created it) or a sender who is its ORGANIZER may change it.

**Meetings.** One `scheduled` meeting (source `invite_mail`) per occurrence that starts within
`ANDAI_INTAKE_HORIZON_DAYS` (14): requester = the verified sender, team = the sender's only team (else
none), title = SUMMARY, `scheduled_start` in UTC, language = `ANDAI_DEFAULT_LANGUAGE`. An invite
that has started but not ended gets a meeting due now (`joined_now`); one that has ended is
ignored. Updates move a meeting, change its link or title, or cancel it — only while it is still
`scheduled`; a skip set in the portal survives every update. The hourly top-up adds occurrences of
recurring invites as they come within the horizon.

**`bot.autoJoin` (D-57).** `off`: mail creates nothing (updates and cancellations of meetings
already scheduled still apply). `only_when_invited`: an auto-forwarded copy (`Auto-Submitted:
auto-forwarded`, an Exchange inbox rule, `X-Forwarded-For`/`-To`) creates nothing. `all_with_link`:
those copies are scheduled too. The team override wins over the org value, as `effective_auto_join`
resolves it when the mail is processed.
