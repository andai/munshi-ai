# Munshi Dashboard

## Why

Users need a visual interface to launch bots into meetings, watch live transcripts with speaker attribution, manage API tokens, and review past meetings. Without the dashboard, all interaction with Munshi requires direct API calls. The dashboard provides the self-service experience for non-technical users and a convenient dev tool for API users.

## What

A Next.js web application that provides:
- Meeting management: launch bots (with optional authenticated mode toggle), see active/past meetings
- Live transcript viewer: real-time segments via WebSocket with speaker labels
- Recording playback: audio player synced with transcript segments
- Browser session management: VNC view, save/clear storage for authenticated bot credentials
- User/token management: create API keys, configure webhooks
- Admin analytics: user and meeting statistics

### Documentation
- [Dashboard UI](../../docs/ui-dashboard.mdx)

### Dependencies

- **api-gateway** -- all API calls route through the gateway
- **admin-api** (via gateway) -- user authentication and token management
- No direct database access -- fully API-driven

## How

See Quick Start and Local Development sections below.

---

Open-source web UI for [Munshi](https://github.com/sunydepalpur/munshi-ai): join meetings, watch live transcripts, manage users/tokens, and review transcript history.

Main backend repo: [Munshi](https://github.com/sunydepalpur/munshi-ai)

## Quick Start (Docker)

```bash
docker run --rm -p 3000:3000 \
  -e MUNSHI_API_URL=http://your-munshi-host:8056 \
  -e MUNSHI_ADMIN_API_KEY=your_admin_api_key \
  ghcr.io/sunydepalpur/munshi-dashboard:latest
```

> **Production:** Use immutable tags (e.g., `0.10.0-260405-0108`) instead of `:latest` for reproducible deployments.

Then open `http://localhost:3000`. (The container listens on port 3000; the `npm run dev` server uses port 3001.)

## Local Development

The dashboard lives in the Munshi monorepo at `services/dashboard/`.

```bash
git clone https://github.com/sunydepalpur/munshi-ai.git
cd munshi/services/dashboard
npm install
cp .env.example .env.local
npm run dev
```

Local dev server runs on `http://localhost:3001`.

## Recording Playback (Post-Meeting)

On completed meetings, the meeting detail page can show an audio playback strip (if a recording exists) and highlight transcript segments during playback. Clicking a segment seeks the audio.

Backend requirements:
- Munshi must expose recordings in the transcript response (so the dashboard can discover recordings without extra calls).
- `GET /recordings/{recording_id}/media/{media_file_id}/raw` should stream audio with `Range` support (`206`) and `Content-Disposition: inline` so browser seeking works.

Notes:
- The dashboard fetches audio through its own `/api/munshi/...` proxy to avoid MinIO/S3 CORS issues.

## URL Proxy Pattern

All gateway requests from the browser go through Next.js server-side proxies — never directly to the gateway URL. This avoids CORS issues and keeps the gateway URL as a single env var (`MUNSHI_API_URL`).

| Browser path | Proxied to | Configured in |
|---|---|---|
| `/api/munshi/*` | `${MUNSHI_API_URL}/*` | `src/app/api/munshi/[...path]/route.ts` |
| `/b/*` | `${MUNSHI_API_URL}/b/*` | `next.config.ts` rewrites |

**Rule:** Components must use relative paths (`/b/{token}/save`, `/api/munshi/meetings`) for fetch calls. The public gateway URL (`publicApiUrl` from `/api/config`) is only for display purposes (e.g., CDP connection strings for external tools).

In Docker Compose, `MUNSHI_API_URL` comes from the root `.env`. In Kubernetes, it comes from a ConfigMap.

## Zoom Notes

Zoom meeting joins require additional setup in the Munshi backend (Zoom Meeting SDK + OAuth/OBF). See the Munshi repo doc: `docs/zoom-app-setup.mdx`.

## Required Configuration

| Variable | Required | Notes |
|---|---|---|
| `MUNSHI_API_URL` | Yes | Munshi API base URL (usually `http://localhost:8056` for local Munshi) |
| `MUNSHI_ADMIN_API_KEY` | Yes | Admin API key used for auth/user management |
| `MUNSHI_ADMIN_API_URL` | No | Optional override; defaults to `MUNSHI_API_URL` |

## Common Optional Configuration

| Area | Variables |
|---|---|
| Session/auth | `NEXTAUTH_URL`, `NEXTAUTH_SECRET`, `JWT_SECRET`, `NEXT_PUBLIC_APP_URL` |
| Magic-link email | `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURE`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM` |
| Google OAuth | `ENABLE_GOOGLE_AUTH`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` |
| Zoom OAuth | `ZOOM_OAUTH_CLIENT_ID`, `ZOOM_OAUTH_CLIENT_SECRET`, `ZOOM_OAUTH_REDIRECT_URI`, `ZOOM_OAUTH_STATE_SECRET` |
| AI assistant | `AI_MODEL`, `AI_API_KEY`, `AI_BASE_URL` |
| Registration policy | `ALLOW_REGISTRATIONS`, `ALLOWED_EMAIL_DOMAINS` |
| Bot defaults | `DEFAULT_BOT_NAME` |
| Hosted mode | `NEXT_PUBLIC_HOSTED_MODE` |
| Frontend/public URLs | `NEXT_PUBLIC_APP_URL`, `NEXT_PUBLIC_BASE_URL`, `NEXT_PUBLIC_TRANSCRIPT_SHARE_BASE_URL`, `NEXT_PUBLIC_WEBAPP_URL` |

See `.env.example` for a complete template.

## Compose Example

```yaml
services:
  munshi-dashboard:
    image: ghcr.io/sunydepalpur/munshi-dashboard:latest  # For production, use immutable tags (e.g., 0.10.0-260405-0108)
    ports:
      - "3000:3000"
    environment:
      MUNSHI_API_URL: http://munshi:8056
      MUNSHI_ADMIN_API_KEY: ${MUNSHI_ADMIN_API_KEY}
```

## Test

```bash
cd services/dashboard
npm install
npm test
```

Runs vitest against `tests/` — covers `parseMeetingInput`, `parseUTCTimestamp`, `cn`, and language utilities.

## Troubleshooting

- Login or admin routes fail: verify `MUNSHI_ADMIN_API_KEY` is valid.
- Dashboard loads but data is empty: verify `MUNSHI_API_URL` is reachable from the container/runtime.
- OAuth callbacks fail: verify `NEXTAUTH_URL` and provider redirect URIs match exactly.
- Transcript page shows no data: verify the dashboard's `MUNSHI_API_KEY` (or user's token) has `tx` scope. Tokens with only `bot` scope can create bots but can't read transcripts (403 on `/transcripts` endpoints).
- Browser session creation fails: verify the token has `browser` scope and concurrent bot limit is not exceeded (error: "Concurrent bot limit reached (N/5)").
- POST /bots returns 422 `[object Object]`: the gateway requires `platform` + `native_meeting_id` fields. The dashboard client-side parses meeting URLs to extract these. If the URL parse fails, the raw URL is sent and the gateway rejects it.

### Verified 2026-04-05 (compose mode)

All backend calls validated from inside the dashboard container:
- GET: gateway root → 200, /meetings → 401 (correct — needs auth), /bots/status → 401, admin /users → 200, agent-api health → 200, internal auth → 200
- POST: /api/munshi/bots (browser_session) → 201, /api/munshi/bots (meeting join) → 201
- Dashboard serving on port 3001 (dev) / 3000 (production Docker)

## Screenshots

![Dashboard](docs/screenshots/01-dashboard.png)
![Join Meeting](docs/screenshots/02-join-meeting.png)
![Live Transcript](docs/screenshots/06-live-transcript.png)

## Related

- Munshi deployment guide
- Munshi Lite deployment guide
- Munshi API guide

## DoD

| # | Check | Weight | Ceiling | Status | Evidence | Last checked | Tests |
|---|-------|--------|---------|--------|----------|--------------|-------|
| 1 | Dashboard loads on port 3000 (Docker) / 3001 (dev) without errors | 20 | ceiling | untested | — | — | — |
| 2 | `MUNSHI_API_URL` reachable and proxy `/api/munshi/*` returns upstream responses | 25 | ceiling | untested | — | — | — |
| 3 | Login flow works (admin key or OAuth) and user session established | 20 | ceiling | untested | — | — | — |
| 4 | Meeting list page loads via `GET /meetings` through gateway | 15 | — | untested | — | — | — |
| 5 | `MUNSHI_ADMIN_API_KEY` set and admin endpoints accessible | 10 | ceiling | untested | — | — | — |
| 6 | `npm test` passes (parseMeetingInput, parseUTCTimestamp, cn, language utils) | 10 | — | untested | — | — | — |

Confidence: 75 (5/6 items covered by tests3: DASHBOARD_UP health check, DASHBOARD_LOGIN contract, dashboard-auth.sh (login+cookie+identity), dashboard-proxy.sh (meetings+pagination+transcript), DASHBOARD_ADMIN_KEY_VALID. -15: npm test not run. -10: DoD items not scored individually against evidence.)

## License

Apache-2.0 (`LICENSE`)
