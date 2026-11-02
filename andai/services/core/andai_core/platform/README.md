# andai/services/core/andai_core/platform

Each org's principal on the meeting platform:

- `admin_client.py` — `HttpPlatformAdmin`, the admin-api client (`PLATFORM_ADMIN_URL`,
  `PLATFORM_ADMIN_TOKEN`): find-or-create a user, mint a scoped token, revoke a token.
- `principals.py` — `provision` (the user `org-<org_id>@principals.munshi.invalid` and a `bot,tx`
  key), `configure` (the user's platform settings: storage prefix `org/<org_id>`, diagnostic tapes
  off, `ANDAI_ORG_MAX_CONCURRENT_BOTS`), `store` (sealed under the KEK into `platform_principals`,
  one row per org), `open_key`, and `sync` (re-applies `configure` to every existing principal;
  `python -m andai_core principals-sync`). The key is never returned by a route or logged.

- `gateway_client.py` — `HttpPlatformGateway`, the gateway client (`PLATFORM_GATEWAY_URL`), called
  with one org's key per call: `dispatch` (`POST /bots`), `list_bots`, `get_meeting`, `stop`,
  `get_transcript` (`GET /transcripts/by-id/{id}`), `get_participants`
  (`GET /meetings/{platform}/{native}/participants`); `live_url` names the gateway's `/ws`, which the
  live relay (`meetings/live.py`) opens with the same key.
  Errors carry the call and status only; a structured duplicate 409 is `DuplicateMeeting`.

- `webhook.py` — `POST /internal/platform-events`, the platform's system webhook (meeting-api's
  `MUNSHI_SYSTEM_WEBHOOK_URL`, pointed here by the compose overlay). Outside `/api`, so the portal
  never proxies it; HMAC-SHA256 over the raw body with `ANDAI_PLATFORM_WEBHOOK_SECRET` and a ±300 s
  timestamp window (503 without a secret, 401 on any mismatch); deduplicated on `event_id` in
  `platform_webhook_events`. A `meeting.completed` only records the event and makes that meeting's
  transcript copy due (`andai_platform_completed`) in one transaction; andai-worker's claims do the
  work. Unknown meetings are answered like known ones.

`andai_core.org.service.create_org` provisions and configures the principal before it inserts the
org, so an org never exists without a configured one.
