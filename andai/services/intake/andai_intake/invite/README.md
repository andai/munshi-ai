# andai/services/intake/andai_intake/invite

Invite parsing for the notetaker mailbox. `parse(raw, notetaker_addresses, default_tz=...)` turns
the stored bytes of one message into `ParsedEvent`s. It is pure: no database, no network, no
scheduling decision. A message it cannot read within its caps raises `InviteParseError`, whose
quarantine reason is `parse_error`.

- `mime.py` — the calendar parts and text bodies of a message (stdlib `email`, `policy.default`).
- `ics.py` — `parse()` and `ParsedEvent`: one entry per VEVENT, deduplicated across parts.
- `tz.py` — TZID → time zone, per message, without icalendar's process-wide cache.
- `recur.py` — `occurrences(event, now, horizon, overrides)`: the instances that matter now, in UTC.
- `links.py` — the join link: where to look, unwrapping, and the host check.
- `forward.py` — the forwarded-copy and auto-forward flags.

## What a parsed event holds

METHOD (REQUEST, CANCEL, REPLY, COUNTER, REFRESH, ADD; anything else, PUBLISH included, is UNKNOWN
and the original is kept in `method_raw`), the UID and `uid_norm`, RECURRENCE-ID (UTC, or a date)
with RANGE, SEQUENCE (0 when absent or unreadable), DTSTAMP, SUMMARY (control, zero-width and bidi
characters removed, at most 200 characters), ORGANIZER, the ATTENDEEs, STATUS, DTSTART and DTEND in
UTC, the zone they were read in, RRULE, RDATE and EXDATE, the join link and its platform, and the
flags below. `warnings` names anything that was assumed (`floating`, `unknown_tzid`,
`vtimezone_mismatch`, …).

`uid_norm` is the UID with surrounding white space removed. For an Exchange Global Object ID (hex
starting `040000008200E00074C5B7101A82E008`), the instance date in bytes 16–19 is zeroed as well, so
a single instance and its series share the key.

## MIME

Calendars come from `text/calendar` and `application/ics` parts, inline or attached, and from one
level of `message/rfc822` ("Forward as attachment"). A message inside that one is ignored. Other
attachments (zip, PDF, HTML, images) are never decoded. Identical calendars are kept once, and
events are deduplicated on `(uid_norm, RECURRENCE-ID, SEQUENCE, DTSTAMP)`, so an inline part and its
`invite.ics` twin give one event.

Caps (each a `parse_error`): 50 MIME parts, nesting depth 5, 1 MiB per calendar part, 200 VEVENTs
per message, and 5 s of work (`deadline_s`, checked between parts and between events; one icalendar
parse of one part is not interrupted). Within an event, 500 ATTENDEEs and 1,000 RDATE/EXDATE values
are kept. A VEVENT without a UID, or with a DTSTART that cannot be read, is skipped.

## Time zones

A TZID resolves to an IANA zone, else a Windows name through icalendar's CLDR-generated
`WINDOWS_TO_OLSON` (`India Standard Time` → `Asia/Calcutta`), else a "globally unique"
`/vendor/Area/City` by its trailing name. When the message embeds a VTIMEZONE for that TZID, its
offsets are compared day by day, from 30 days before the event to 400 days after it, with the mapped
zone. If they disagree, or nothing maps (Outlook's "Customized Time Zone"), the VTIMEZONE's own
offsets are used, cached by content hash and never by TZID. icalendar's own cache is emptied after
every parse. An unknown TZID with no VTIMEZONE, and a floating time (no TZID, no `Z`), are read in
`ANDAI_DEFAULT_TIMEZONE` and flagged. An all-day event (`VALUE=DATE`) has no start time and never
produces an occurrence.

## Recurrence

`occurrences()` expands in the event's zone and wall-clock time (dateutil `rrule`), so a 10:00
meeting stays at 10:00 across DST, then converts to UTC. It returns the instances that have not
ended by `now` and start by `now + horizon` (14 days by default, D-58). An instance already running
is returned too, for the join-at-once rule. RDATE adds instances, EXDATE removes them, and
RECURRENCE-ID overrides move an instance or, with STATUS:CANCELLED, remove it. A VEVENT with a
RECURRENCE-ID and no RRULE that arrives alone is that one instance.

Bounds: SECONDLY, MINUTELY and HOURLY rules, and series starting before 1970, expand to nothing
(`refused`). So does a DAILY rule with BYMONTH, BYMONTHDAY, BYYEARDAY, BYWEEKNO or BYSETPOS, because
dateutil walks a rule that never matches again to the year 9999 (about 2 s), and no cap can
interrupt that. UNTIL is clipped to the window end. The walk stops after 1,000 instances in the
window, 50,000 in all, or 0.5 s (`capped`). A barren WEEKLY, MONTHLY or YEARLY rule still takes up
to about 0.5 s.

## Join links

Order: `X-GOOGLE-CONFERENCE`, `X-MICROSOFT-SKYPETEAMSMEETINGURL`,
`X-MICROSOFT-ONLINEMEETINGEXTERNALLINK`, `DESCRIPTION`, `X-ALT-DESC`, `LOCATION`, then the mail's
text and HTML bodies. In the text sources, a link shaped like a join link (Meet `/abc-defg-hij`,
Teams `/l/meetup-join/…` or `/meet/…`, Zoom `/j/`, `/w/`, `/my/`, `/wc/`) wins over other links to
the same hosts, such as Teams "Meeting options" or Zoom "Find your local number". When there is no
join-shaped link, the first link that passes the checks is used.

Wrappers removed, at most 3 levels, without fetching anything:

| Wrapper | Form | Decoding |
| --- | --- | --- |
| Microsoft Defender Safe Links | `*.safelinks.protection.outlook.com/?url=` | the `url` parameter |
| Proofpoint v1 | `urldefense.proofpoint.com/v1/url?u=` | the `u` parameter |
| Proofpoint v2 | `urldefense.proofpoint.com/v2/url?u=` | `-` → `%`, `_` → `/`, then percent-decoding |
| Proofpoint v3 | `urldefense.com/v3/__<url>__;<base64>!…` | `*` and `**X` take characters from the base64 data |
| Google redirect | `www.google.com/url?q=` | the `q` parameter |
| Mimecast | `*.mimecast.com`, `*.mimecastprotect.com` | opaque: no link; the next candidate is tried |

The Proofpoint v3 decoder is a port of the public-domain (CC0-1.0) decoder at
https://github.com/cardi/proofpoint-url-decoder. Proofpoint's own script is GPL-3.0 and is not used.

After unwrapping, a link must be `https`, without user info, with no port other than 443, and with
no white space, control character or backslash. Its host is IDNA-encoded and must pass
`andai_core.meetings.service.link_platform()`, the same allowlist the portal's paste-link dispatch
uses, so look-alike and homograph hosts are refused. The path and query are kept (Teams `?p=`, Zoom
`pwd=`); the fragment is dropped.

## Forwarded copies and auto-forwards

`is_forwarded_copy` is true when no notetaker address is an ATTENDEE. Someone forwarded a copy, and
the organizer may never send it an update or cancellation.

`auto_forwarded` is true when the message carries one of these headers:

- `Auto-Submitted: auto-forwarded`
- `X-MS-Exchange-Inbox-Rules-Loop` (Exchange / Outlook inbox rule)
- `X-Forwarded-For` or `X-Forwarded-To` (Gmail and Postfix-style forwarding)

`via_forward_attachment` is true for events found inside a `message/rfc822` part. Their ORGANIZER is
whatever the inner message says; only the outer sender is verified.
