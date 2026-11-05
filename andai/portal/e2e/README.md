# andai/portal/e2e

Playwright browser tests for the portal against a disposable compose stack (project `andai-e2e`):
`global-setup.ts` checks the stack's host ports are free (stop the dev stack first: `make -C andai
down`, or give the e2e env file its own ports), rebuilds the andai-core, andai-worker, andai-portal
and mock-bot images at the env file's `IMAGE_TAG` (give the e2e env file a tag of its own, so the
build never moves the images another stack runs), runs `make up` with the mock bot as the meeting
bot (`BROWSER_IMAGE=mock-bot:dev`) and bootstraps the first organisation through the portal;
`global-teardown.ts` removes the stack with its volumes and the bot containers it started. Specs:
`smoke` (sign in/out, dark theme), `auth` (invite, reset, rate limit), `admin` (teams, roles,
disabling, 403, team lead), `intake` (below), `twofa` (voluntary enrollment, lockout, recovery codes,
require-two-factor, admin reset), `meetings` (send the bot, live status and transcript, lobby
instructions, failures, polling when the stream drops, a colleague's meeting, another organisation's
404) and `dashboard` (the meeting list's sections, filters and Load more, the Unassigned action items
between Upcoming and Past and their links to the cited line, what a member may see before and
after a share is revoked, keyword search with snippets including a Roman query for
Devanagari speech, and the first-list time with 1,000 meetings, desktop only) and `meeting-detail`
(a finished meeting's transcript in both scripts, attendees, minutes whose evidence links scroll to
and highlight the cited line, a search result opening at its line, sharing with a team and removing
it as requester and team lead, what a grantee sees, and deletion) and `portal-core` (the whole chain
on one meeting in an organisation of its own: the join form sends the mock bot to a Hinglish meeting
(`mock=hindi-lines`), andai-worker stores the transcript and writes its minutes, a Roman query finds the Devanagari speech,
both scripts, a team share and its removal, seeded minutes' evidence, and a delete purged from
andai's tables and from storage within 15 minutes; `E2E_CHAIN_LOG=/abs/file` appends each run's
timings and counts as one JSON line). The dashboard and meeting-detail
specs seed their meetings, shares, transcript segments, attendees and minutes in an organisation of
their own, written inside the stack's andai-core container (`seedMeetings`, `seedMinutes`, sample
names only); `meetingRows` and `recordingObjectCount` read a meeting's rows (psql in the postgres
container) and the objects under an org's recordings prefix (`find` in the storage container), with no
credential leaving either container. Meeting links are
Zoom-shaped with a unique number per test and `mock=<scenario>` for the mock bot; the second
organisation is created inside the stack's andai-core container (`docker exec`), since bootstrap
runs once. Projects `desktop` (1440×900) and `mobile` (390×844).

The minutes pipeline runs on the e2e stack: global-setup adds the `e2e` compose profile, whose
`fake-llm` service (a deterministic OpenAI-compatible route, `andai_core.testing.fake_llm`) writes
the minutes of every finished meeting through andai-worker, so the env file must name it as the LLM
route (`ANDAI_LLM_BASE_URL=http://fake-llm:8000`, no `/v1`, and any `ANDAI_LLM_MODEL`); setup refuses
otherwise, and by default no test reaches a real model. Seeded meetings keep the pipeline and the recap mail
away: their minutes are `stored` (when seeded) or `skipped`, their recap `skipped`, unless a seed
names `momState` (`pending` hands a meeting to the pipeline after `momDueInS` seconds; `failed`
carries `momError`). `minutes` covers the minutes on the meeting page: preparing, then version 1
without a reload; a typed failure and Regenerate; nothing said; an edit saved as a new version (an
uncited item blocked, andai-core's 422 shown); the version menu and the unsaved-edit guard;
regenerating in another template and language (Hindi headings); a grantee's Markdown and PDF
downloads. `mom-pipeline` runs the pipeline end to end on one meeting in an organisation of its own:
the join form sends the mock bot to a Hinglish meeting with a team, the platform's webhook arrives
once, the transcript and version 1 are stored and appear on the open page, one recap mail reaches
exactly the requester and the team (`mailsMentioning`), an edit and a regenerate (Indian formal MoM
in Hindi) add versions 2 and 3 and send nothing, version 3 downloads as Markdown and PDF (copies in
`test-results/downloads/`), a grantee reads it, and the dashboard's Unassigned section lists the
latest version's unassigned items. A second test stops andai-core and andai-worker while the bot
records (`stopServices`), lets the platform end the meeting (`platformStatus`), starts them again and
checks one version and one recap. Times come from andai's database (`pipelineRow`);
`E2E_CHAIN_LOG=/abs/file` appends them as JSON lines. `E2E_REAL_LLM_ENV_FILE=/abs/file` (only
`ANDAI_LLM_*` keys) recreates andai-core and andai-worker on that route for the chain alone
(`useLlmRoute`; values never printed) and back on the fake route afterwards: use it only with the
mock bot's synthetic lines and a route you are allowed to send them to.

Invite intake runs on the e2e stack too: the `e2e` profile also starts `fake-dns`
(`andai_intake.testing.fake_dns`), a stub DNS server in andai-intake's network namespace, so the env
file must set `ANDAI_INTAKE_DNS_RESOLVER=127.0.0.1:5353` (setup refuses otherwise) and the sender
check never asks a real resolver. `smtp.ts` builds a synthetic calendar invitation (`inviteMail`) and
`deliver`s it inside the fake-dns container: `andai_intake.testing.sign` publishes the From domain's
SPF and DMARC records and, when signed, its DKIM key, then `andai_intake.replay` sends it to
andai-intake's receiver. `intake` covers, in an organisation of its own whose test domain it claims
(`setOrgEmailDomains`): an admin setting auto-join for the organisation and a team on `/admin/bot`
(a member gets the 403 page there and on `/admin/intake`); a forged invite and one from a non-member
listed on `/admin/intake` with plain reasons and the sender check, and a forged invite naming
another organisation's member not listed; a member's signed invite under Upcoming at its start in
the viewer's zone (the page runs in America/New_York), the confirmation in Mailpit, Skip marking the
row and un-skipping restoring it, and the mock bot joining two minutes before the start (the row
moves to Live; about two minutes of real time per project); and with auto-join off, a new invite
scheduling nothing, with a confirmation that says why.

```bash
cd andai/portal
npx playwright install chromium
E2E_ENV_FILE=/abs/path/to/env npx playwright test --reporter=list
```

`E2E_SKIP_BUILD=1` reuses the images already built; `E2E_KEEP_STACK=1` leaves the stack up
afterwards (remove it with `docker compose -p andai-e2e down -v`). Screenshots go to
`test-results/screenshots/` (light and dark, per project) and the HTML report to `playwright-report/`
(both git-ignored, local only).
