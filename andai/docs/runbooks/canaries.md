# Canaries, join metrics and alert mail

munshi.ai finds out that bots stopped getting into meetings before its users do. andai-worker sends
a **canary** bot into a standing test meeting on each platform at least every 4 hours. It exports
join metrics at `/metrics` and mails the operator when joins start failing (BOT-04, D-34). No
Prometheus, Alertmanager or Grafana server ships with munshi.ai; scrape `/metrics` with whatever
the deployment already runs.

Code: `andai/services/worker/andai_worker/` (`canary.py`, `metrics.py`, `alerts.py`), the canary
org in `andai_core/meetings/service.py`, and the tables and functions in migration
`0005_canaries.py`.

## How a canary runs

1. `make -C andai canary-setup` creates the **canary org** once. It is an ordinary org flagged
   `is_canary`, with its own platform principal (a cap of 3 bots, one per platform) and an owner
   that has no password and never gets mail. Canary bots never use a customer org's bot cap, and
   no customer sees a canary meeting. The canary org sends nothing but canaries, and only the
   canary org may send them; andai-core refuses anything else with 403 `canary_org_only`.
2. For each platform with a link set, one `canary_runs` row is always scheduled or running. A run
   is due `ANDAI_CANARY_INTERVAL_S` after the previous one started (at most 14400 s).
3. When a run is due, a worker claims it (`FOR UPDATE SKIP LOCKED`, so with several workers each
   run starts once). The worker sends the bot through the same dispatch path as the portal,
   `Meetings.join`, as the canary org, with source `canary`. Canaries are exempt from the 12-hour
   no-rejoin rule (D-38).
4. andai-core's reconciler keeps the canary meeting's status current. A canary **succeeds** when
   the bot is admitted (it reached `recording`). The worker then stops the bot 60 s later, so a
   canary never holds a bot slot. A canary **fails** when its meeting fails, and keeps the
   meeting's `JoinFailureReason`. A canary with no answer after the lobby timeout plus 5 minutes
   fails as `admission_timeout`, and its bot is stopped.

The worker ticks once a minute, or every quarter of the interval when that is shorter (a 20 s
test interval ticks every 5 s).

## 1. Standing test meetings

Each canary link must be a meeting that **admits a guest with no human present**, for as long as
the deployment runs. Use a test account, never a customer's or a colleague's real meeting room.

| Platform | Meeting to create | Settings that let the bot in alone |
|---|---|---|
| Google Meet | A meeting in a test Google account or Workspace, e.g. a calendar event with no end date | Host management on, with **Meeting access: Open** ("No one has to ask to join"). If the Workspace restricts access to in-domain users, the anonymous bot cannot join at all. |
| Microsoft Teams | A meeting in a test tenant, organised by a test user (a `/meet/` link works) | Meeting options: **Who can bypass the lobby: Everyone**. The organiser's meeting policy must allow anonymous users to join. Set the tenant's external-bot policy so bots are not held for approval (see `tenant-admission.md`). |
| Zoom | A recurring meeting with no fixed time in a test account | **Waiting room off**. **Only authenticated users can join** off. **Allow participants to join before host** on, at any time. Keep the passcode in the link (`?pwd=`). |

Whether every platform admits an anonymous guest when no host has joined is not proven for all
three. Check each link once by hand: open it in a private browser window, join as a guest, and see
that you are let in with nobody else present. Record a platform that cannot do this as a canary gap.
Its canaries then fail with `admission_timeout` or `admission_denied`, which is expected and alerts.
Known so far:

- Zoom on munshi.ai's own test account showed an anti-bot wall ("automated bots aren't allowed") to the bot.
  That is `blocked_by_policy`.
- Teams has not yet been observed with a host account of munshi.ai's own.

## 2. Settings

In the deployment's env file (the root `.env.example` documents each key):

| Key | Meaning |
|---|---|
| `ANDAI_CANARY_MEET_URL`, `ANDAI_CANARY_TEAMS_URL`, `ANDAI_CANARY_ZOOM_URL` | The standing test meeting per platform; empty turns that platform's canary off. The links may hold passcodes: they are secrets of the deployment and are never logged. |
| `ANDAI_CANARY_INTERVAL_S` | Seconds between two canaries on a platform; default and maximum 14400 (4 h). andai-worker refuses to start with a larger value. |
| `ANDAI_ALERT_EMAIL` | The operator address alert mail goes to; empty turns alert mail off. |
| `SMTP_*` | The relay (shared with andai-core; Mailpit in development). |

Then:

```sh
make -C andai up                 # recreates andai-worker with the new settings
make -C andai canary-setup       # once per deployment; running it again changes nothing
```

andai-worker reads the canary links at start-up, so change them with `make -C andai up` (it
recreates only andai-worker).

## 3. Reading `/metrics`

`curl -s http://127.0.0.1:${ANDAI_WORKER_PORT:-18301}/metrics` (bound to 127.0.0.1 only). Every scrape
reads the andai database. Labels are enums only: no link, name, email, org or id ever appears.

| Series | Meaning |
|---|---|
| `munshi_bot_joins_total{platform,source,outcome,reason}` | Joins started in the last 24 h. `outcome`: `success` (reached `recording`), `failure` (with its `reason`), `excluded` (cancelled by the user, `admission_denied` or `removed_by_participant`: human decisions, D-39), `pending` (still on its way in). `source` is `portal_link`, `canary`, ... |
| `munshi_join_success_ratio_24h{platform,source}` | successes / (successes + failures) over 24 h; `source="all"` is what the rate alert uses. |
| `munshi_canary_consecutive_failures{platform}` | Canary failures in a row up to the latest canary. |
| `munshi_canary_last_run_timestamp_seconds{platform,outcome}` | Start of the latest canary with that outcome. A `success` older than the interval plus a few minutes means canaries stopped running. |

The worker reads across orgs only through `andai_join_stats(since)`, a `SECURITY DEFINER` function
owned by the NOLOGIN role `andai_scan`. It returns `(platform, source, outcome, reason, count)` and
nothing else. As `andai_app` the worker sees no meeting row.

## 4. Alert mail

One evaluator per deployment (a Postgres advisory lock) checks each platform every tick:

- **Canary failures**: the latest 2 canaries on the platform both failed. The incident closes when
  a canary succeeds.
- **Join rate**: the 24 h success rate of all sources together is below 95%, counted per D-39,
  with at least 5 counted joins. It closes when the rate is back at 95%, or fewer than 5 joins
  remain in the window.

Each incident mails `ANDAI_ALERT_EMAIL` **once when it opens** (subject `[munshi.ai] Bot joins
failing on <platform>`) and **once when it closes** (`[munshi.ai] Recovered: ...`). The mail lists
the latest canary runs with their failure reasons and the 24 h counts, never a meeting link or a
customer. A mail the relay refuses stays owed in `alert_state.notice` and goes out on a later tick:
a relay outage delays it, and it is never lost or sent twice.

When an alert arrives:

1. Read `munshi_bot_joins_total` for the failing `reason`. `blocked_by_policy` and `ui_changed`
   point at the platform (a new bot gate, a changed join page). `meeting_not_found` points at the
   standing meeting. `auth_required` means the meeting now requires sign-in. `internal_error`
   points at munshi.ai itself.
2. Open the standing meeting yourself as a guest. If it no longer admits guests, fix its
   settings (section 1).
3. If the platform changed, fix the bot and ship it alone (`bot-hotfix.md`). Then watch the next
   canary: `munshi_canary_last_run_timestamp_seconds{outcome="success"}` moves and the recovery
   mail arrives.

## 5. Proving it without a real meeting

The live test runs the canary and alert loops against a running mock-bot stack. Two
`mock=reject` canaries in a row produce one alert mail in Mailpit. A `mock=normal` canary then
produces the recovery mail, and the container's `/metrics` shows the counts:

```sh
ENV_FILE=/abs/env ANDAI_REQUIRE_DOCKER=1 uv run --directory andai pytest -q -s -m live \
    services/worker/tests/test_worker_canary_live.py
```

Run it only against a test stack under its own compose project (`PROJECT=`), never against a
deployment.
