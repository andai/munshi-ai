# andai/services/core/andai_core/meetings

Sending the bot to a meeting and following it:

- `routes.py` — `POST /api/v1/meetings/join`, `GET /api/v1/meetings/{id}`,
  `POST /api/v1/meetings/{id}/stop`, `GET /api/v1/meetings/{id}/transcript`,
  `GET /api/v1/meetings/{id}/live` (access per `.work/AUTH_MATRIX.md`: requester, team lead,
  admins; 404 for anyone else).
- `service.py` — link family check, one live bot per link per org, dispatch with the org's key and
  its fixed defaults (bot name, language, lobby and alone timeouts, recording notice), the 12-hour
  no-rejoin rule after a removal (D-38), stop, and the API view (passcode only for the requester
  and admins).
- `status_map.py` — the platform's status, completion reason and join evidence → andai status,
  `JoinFailureReason` and end reason; admit instructions per platform.
- `consent.py` — the consent record of each meeting the bot was admitted to.
- `reconciler.py` — the single-runner loop (advisory lock) that polls the platform every 2 s per org
  with open meetings and writes status and consent changes.
- `transcript.py` — the transcript in andai's segment shape (`id`, `speaker`, `start`, `end`,
  `text`, `language`, `final`, absolute times), read from the platform with the org's key; the
  segment normaliser the live relay shares.
- `live.py` — the live relay: server-sent events with status, segments and retractions for one
  viewer, from one gateway WebSocket that andai-core holds with the org's key.
