# andai/portal/e2e

Playwright browser tests for the portal against a disposable compose stack (project `andai-e2e`):
`global-setup.ts` checks the stack's host ports are free (stop the dev stack first:
`make -C andai down`, or give the e2e env file its own ports), rebuilds the andai-core,
andai-portal and mock-bot images at the env file's `IMAGE_TAG`, runs `make up` with the mock bot as
the meeting bot (`BROWSER_IMAGE=mock-bot:dev`) and bootstraps the first organisation through the
portal; `global-teardown.ts` removes the stack with its volumes and the bot containers it started.
Specs: `smoke` (sign in/out, dark theme), `auth` (invite, reset, rate limit), `admin` (teams, roles,
disabling, 403, team lead), `twofa` (voluntary enrollment, lockout, recovery codes,
require-two-factor, admin reset) and `meetings` (send the bot, live status and transcript, lobby
instructions, failures, polling when the stream drops, a colleague's meeting, another
organisation's 404). Meeting links are Zoom-shaped with a unique number per test and
`mock=<scenario>` for the mock bot; the second organisation is created inside the stack's
andai-core container (`docker exec`), since bootstrap runs once. Projects `desktop` (1440×900) and
`mobile` (390×844).

```bash
cd andai/portal
npx playwright install chromium
E2E_ENV_FILE=/abs/path/to/env npx playwright test --reporter=list
```

`E2E_SKIP_BUILD=1` reuses the images already built; `E2E_KEEP_STACK=1` leaves the stack up
afterwards (remove it with `docker compose -p andai-e2e down -v`). Screenshots go to
`test-results/screenshots/` (light and dark, per project) and the HTML report to `playwright-report/`
(both git-ignored, local only).
