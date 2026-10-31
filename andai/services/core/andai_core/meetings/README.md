# andai/services/core/andai_core/meetings

Sending the bot to a meeting, following it, listing and sharing meetings:

- `routes.py` — `GET /api/v1/meetings` (the list), `POST /api/v1/meetings/join`,
  `GET /api/v1/meetings/{id}`, `POST /api/v1/meetings/{id}/stop`,
  `GET /api/v1/meetings/{id}/transcript`, `GET /api/v1/meetings/{id}/live`,
  `GET/POST /api/v1/meetings/{id}/shares` and `DELETE /api/v1/meetings/{id}/shares/{share_id}`
  (access per `.work/AUTH_MATRIX.md`; 404 for anyone who may not read the meeting).
- `access.py` — the one access predicate, `accessible(actor)`, used as SQL by every read path
  (detail, transcript, live, list): admins and owners, the requester, a lead of the meeting's
  team, and the users and current team members it is shared with. `can_read`, `can_stop` and
  `can_share` (requester, team lead, admins, owners — not grantees), `can_delete` (requester,
  admins, owners), the view's `permissions`, and who a meeting is shared with.
- `listing.py` — the dashboard list: Live / Upcoming / Past sections by status, newest first,
  filters `all | mine | shared | team:<id>`, keyset pages with an opaque cursor; access filtered in
  SQL before the limit, no totals.
- `shares.py` — grant and revoke read access to a user or team of the caller's org, each with its
  `share.grant` / `share.revoke` audit event in the same transaction; grantees outside the org or
  unknown are 404 `no_such_grantee`.
- `service.py` — link family check, one live bot per link per org, dispatch with the org's key and
  its fixed defaults (bot name, language, lobby and alone timeouts, recording notice), the 12-hour
  no-rejoin rule after a removal (D-38), stop, and the API view (passcode only for the requester
  and admins; also `title`, `attendees`, `transcript_state`, team and requester names, the
  caller's `permissions`, and `shared_with` for those who may share). Source `canary` only from the
  canary org, which sends nothing else; `canary_actor` is how andai-worker acts as that org.
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
  - andai-worker runs it for each meeting that `andai_claim_ingest()` hands out.
- `live.py` — the live relay: server-sent events with status, segments and retractions for one
  viewer, from one gateway WebSocket that andai-core holds with the org's key. Access is checked
  again at every keepalive; a viewer who lost it gets `access_revoked` and the stream ends.
