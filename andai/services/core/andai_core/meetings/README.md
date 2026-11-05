# andai/services/core/andai_core/meetings

Sending the bot to a meeting, following it, listing, sharing, reading its minutes and deleting it:

- `routes.py` — `GET /api/v1/meetings` (the list), `POST /api/v1/meetings/join`,
  `GET /api/v1/meetings/{id}`, `DELETE /api/v1/meetings/{id}`, `POST /api/v1/meetings/{id}/stop`,
  `GET /api/v1/meetings/{id}/transcript`, `GET /api/v1/meetings/{id}/live`,
  `GET/PUT /api/v1/meetings/{id}/mom`, `GET /api/v1/meetings/{id}/mom/versions`,
  `POST /api/v1/meetings/{id}/mom/regenerate`, `GET /api/v1/meetings/{id}/mom/export.md` and
  `export.pdf`, `GET/POST /api/v1/meetings/{id}/shares` and
  `DELETE /api/v1/meetings/{id}/shares/{share_id}`, `POST/DELETE /api/v1/meetings/{id}/skip`
  (access per `.work/AUTH_MATRIX.md`; 404 for anyone who may not read the meeting), and
  `GET /api/v1/action-items/unassigned`.
- `access.py` — the one access predicate, `accessible(actor)`, used as SQL by every read path
  (detail, transcript, live, minutes, list, search): admins and owners, the requester, a lead of
  the meeting's team, and the users and current team members it is shared with. A deleted meeting
  is readable by no one. `can_read`, `can_stop` and
  `can_share` (requester, team lead, admins, owners — not grantees), `can_delete` (requester,
  admins, owners), the view's `permissions`, and who a meeting is shared with.
- `listing.py` — the dashboard list: Live / Upcoming / Past sections by status (Live and Past
  newest first, Upcoming soonest `scheduled_start` first), filters `all | mine | shared | team:<id>`,
  keyset pages with an opaque cursor; access filtered in SQL before the limit, no totals.
  `has_minutes` says whether a minutes version exists; each row also carries `source`,
  `scheduled_start`, `skipped` and `permissions.can_share`, computed in the same query
  (`access.may_share`).
- `scheduled.py` — meetings scheduled from invites: the skip toggle (`can_share` callers, 409
  `not_scheduled` once the meeting is no longer scheduled, audited `meeting.skip` /
  `meeting.unskip`), and the writes andai-intake makes as the sender's org (`insert_scheduled`,
  `update_scheduled`, `cancel_scheduled`), which touch a meeting only while it is still scheduled.
  Scheduled meetings do not hold their link in the one-live-bot-per-link index.
- `action_items.py` — the dashboard's Unassigned section: the action items whose owner is
  `unassigned` (any case) in the latest minutes version of each meeting `accessible(actor)` admits,
  newest meeting first then in the minutes' order, keyset pages of 25 (at most 50) with an opaque
  cursor. Access, the latest-version join (one step down the `mom_documents` primary key) and the
  owner test are SQL, before the limit. Each item carries its meeting's title, platform, times and
  team, and the first segment it cites (`evidence_first_id`) for the `#seg-<id>` link.
- `mom.py` — minutes of meeting: the latest version of a meeting's SPEC
  `MomDocument`, or any named version, for its readers (404 `no_minutes` when there is none,
  `no_such_version`), with its template's `sections`, `headings` and `labels` in its output
  language (`layout`, from `minutes.templates`, shared with the exports); the version list (template,
  language, model, generated or edited at and by). Its evidence ids are transcript segment ids.
  `seed_version` validates a document against the schema and the meeting's stored segments before
  storing the next version (tests and seeds). Versions are never changed (the app role may only
  SELECT and INSERT `mom_documents`).
- `mom_edit.py` — changing the minutes, for the requester, the meeting's team lead, admins and
  owners (`can_share`; 404 for anyone else): `save_edit` stores an edit as the next version through
  `write_version(strict=True, edited_by=…)` (every item must cite segments of the meeting, else 422
  `invalid_minutes` naming the item; a stale `baseVersion` is 409 `version_conflict`);
  `request_regenerate` sets `mom_state = pending` with `mom_request` (template, language, who,
  when) for the minutes job, never touching a running job's lease (409 `minutes_pending` while a
  request waits, `transcript_not_ready` before the transcript is stored). Audited `mom.edit` and
  `mom.regenerate` in the same transaction.
- `mom_writer.py` — the one write path for minutes versions, generated or edited:
  `build_document` (no database) keeps only items citing segments of the meeting (generated drafts
  drop the rest and count them; edits are refused with the section and index), applies the owner
  rule and resolves due dates for generated drafts, and fills ids, version, profile, model and
  times; `write_version` stores the result as the next version under the org transaction, through
  the same insert as `seed_version`, retrying once on a concurrent version and refusing a stale
  `base_version` (`VersionConflict`).
- `minutes_job.py` — one run of a meeting's minutes job, for each meeting `andai_claim_mom()` hands
  andai-worker with a lease token: load the stored transcript (`text_raw`), attendees, local date and
  dispatch language (or a pending regenerate's `mom_request`); an empty transcript ends `skipped`;
  otherwise generate the draft and store it through `write_version` under the row lock, only while
  the meeting is undeleted and still holds the lease. Sets `mom_state = stored`, `mom_at` (first
  minutes) and `recap_state = pending` for the first version; a regenerate written meanwhile leaves
  it `pending` for the next claim. Typed failures go to `mom_error` and back off (30 s doubling,
  at most 30 min); `failed` after 8 attempts.
- `recap.py` — the recap mail of a meeting's first minutes, for each recap `andai_claim_recap()`
  hands andai-worker with a lease token: one plain-text mail (summary, decisions, action items with
  owner and due date, "None recorded" for an empty section, the portal link `/meetings/<id>`) in the
  minutes' output language with the template's headings, always of version 1, to the requester and
  the active members of the meeting's team and nobody else (D-48). `send_one` re-checks the lease
  under the row lock, sends before the commit that marks it `sent` (at-least-once only across a
  crash in between); relay errors back off (30 s doubling), `failed` after 5; a deleted meeting, no
  minutes or no recipient end `skipped`. Regenerated and edited versions never send.
- `title.py` — `set_title`, the only writer of a meeting's title: it stores the title and its fold
  (`title_search`) together.
- `shares.py` — grant and revoke read access to a user or team of the caller's org, each with its
  `share.grant` / `share.revoke` audit event in the same transaction; grantees outside the org or
  unknown are 404 `no_such_grantee`.
- `service.py` — link family check, one live bot per link per org, dispatch with the org's key and
  its fixed defaults (bot name, language, lobby and alone timeouts, recording notice), the 12-hour
  no-rejoin rule after a removal (D-38, from consent records and removal markers), stop, and the API view (passcode only for the requester
  and admins; also `title`, `attendees`, `transcript_state`, `mom_state` and `mom_error`, team and requester names, the
  caller's `permissions`, and `shared_with` for those who may share). Source `canary` only from the
  canary org, which sends nothing else; `canary_actor` is how andai-worker acts as that org.
  `start_scheduled` dispatches a due scheduled meeting (claimed by andai-worker through
  `andai_claim_scheduled`, migration 0016) as its requester (`requester_actor`) through the same
  checks and steps as a paste, on the existing row: a live meeting of the same link or a D-38
  removal ends it `cancelled` with `end_reason` `already_in_meeting` / `removed_by_participant`, a
  platform 422 fails it `meeting_not_found`, no platform principal leaves it scheduled for the
  claim's backoff (`failed` after 5 claims); a skipped meeting is never dispatched.
  `drop_scheduled` cancels one whose requester is no longer an active member.
- `deletion.py` — `request_delete` (requester, admins, owners; 409 `meeting_live` while the bot is
  in the meeting) hides the meeting and writes `meeting.delete` in one transaction; `purge`, run by
  andai-worker for each deletion `andai_claim_deletions()` hands out, deletes the platform's
  recording objects and transcript with the org's key (the platform keeps a tombstone row), then
  andai's segments, shares, consent record, minutes and meeting row, keeping only the audit events
  and, for a meeting participants removed the bot from in the last 12 hours, a content-free
  `meeting_removals` marker (D-38). Retries back off from 10 s to 2 min; `failed` after 30.
- `status_map.py` — the platform's status, completion reason and join evidence → andai status,
  `JoinFailureReason` and end reason; admit instructions per platform.
- `consent.py` — the consent record of each meeting the bot was admitted to.
- `reconciler.py` — the single-runner loop (advisory lock) that polls the platform every 2 s per org
  with open meetings and writes status and consent changes.
- `transcript.py` — the transcript in andai's segment shape (`id`, `speaker`, `start`, `end`,
  `text`, `language`, `final`, absolute times), read from the platform with the org's key; the
  segment normaliser the live relay shares. Once andai holds its own copy of a finished meeting
  (`transcript_state = stored`) the route serves that copy, whose segments also carry `text_raw`,
  `text_canonical` (Roman Hinglish) and `script`.
- `ingest.py` — andai's own copy of a finished meeting:
  - It reads the final transcript and the participants with the org's key and stores each segment
    as raw, canonical and folded search text (`andai_core.text`), together with the
    de-duplicated attendees (invitees, then speakers). The write is one transaction and is
    idempotent.
  - An empty transcript is retried with backoff, as is a platform error; a platform error ends in
    `failed` after 8 attempts.
  - A deleted meeting is never ingested, and nothing is stored for one deleted mid-ingest.
  - andai-worker runs it for each meeting that `andai_claim_ingest()` hands out.
- `live.py` — the live relay: server-sent events with status, segments and retractions for one
  viewer, from one gateway WebSocket that andai-core holds with the org's key. Access is checked
  again at every keepalive; a viewer who lost it gets `access_revoked` and the stream ends.
