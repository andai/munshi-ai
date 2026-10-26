# andai/portal/e2e

Playwright browser tests for the portal against a disposable compose stack (project `andai-e2e`):
`global-setup.ts` checks the stack's host ports are free (stop the dev stack first:
`make -C andai down`), rebuilds the andai-core and andai-portal images, runs `make up` and
bootstraps the first organisation through the portal; `global-teardown.ts` removes the stack with
its volumes. Specs: `smoke` (sign in/out, dark theme), `auth` (invite, reset, rate limit), `admin`
(teams, roles, disabling, 403, team lead) and `twofa` (voluntary enrollment, lockout, recovery
codes, require-two-factor, admin reset). Projects `desktop` (1440×900) and `mobile` (390×844).

```bash
cd andai/portal
npx playwright install chromium
E2E_ENV_FILE=/abs/path/to/env npx playwright test --reporter=list
```

`E2E_SKIP_BUILD=1` reuses the images already built; `E2E_KEEP_STACK=1` leaves the stack up
afterwards (remove it with `docker compose -p andai-e2e down -v`). Screenshots go to
`test-results/screenshots/` (light and dark, per project) and the HTML report to `playwright-report/`
(both git-ignored, local only).
