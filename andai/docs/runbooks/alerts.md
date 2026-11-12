# Alerts

What each munshi.ai alert means, what to check first and how to silence it. The rules are
`andai/deploy/monitoring/prometheus/rules/munshi-alerts.yml`; how to turn monitoring on and route
alerts to email, Slack or Microsoft Teams is in `andai/docs/monitoring.md`.

Every notification carries only the alert name, the service, the severity and a fixed one-line
summary, for example:

```
[FIRING] MunshiPipelineBacklog — pipeline
1 firing, 0 resolved
- warning: Pipeline backlog older than 15 minutes (firing)
```

It never names a meeting, an org, a person, an address or a link, and it does not say which
platform, stage or disk: read that from the dashboards (Perses, `127.0.0.1:${PERSES_PORT:-19080}`)
or Prometheus (`127.0.0.1:${PROMETHEUS_PORT:-19090}`), both reachable on the host only. A resolved
alert sends one `[RESOLVED]` notification.

Find the series behind an alert in Prometheus' query page with the rule's expression, for example
`max by (stage) (munshi_pipeline_oldest_pending_seconds) > 900`. The metric catalogue is
`andai/docs/metrics.md`.

## Silencing

A silence stops notifications for matching alerts for a time; the alert itself keeps firing in
Prometheus. Open Alertmanager on the host (`http://127.0.0.1:${ALERTMANAGER_PORT:-19093}`), choose
**New Silence**, match on `alertname` (and `service` when one alert name covers several), set the
duration and a comment saying why. From the command line, inside the Alertmanager container:

```sh
docker compose -p <project> exec alertmanager amtool --alertmanager.url=http://localhost:9093 \
  silence add alertname=MunshiPipelineBacklog --duration=2h --comment="minutes route down, CTO aware"
docker compose -p <project> exec alertmanager amtool --alertmanager.url=http://localhost:9093 silence query
docker compose -p <project> exec alertmanager amtool --alertmanager.url=http://localhost:9093 silence expire <id>
```

Silence for as short a time as the fix needs, and never put a person's name or address in the
comment.

## MunshiCanaryFailing

**Means:** two or more canary joins in a row failed on one platform (`service: bot-joins`,
critical). The bot cannot get into a standing test meeting, so real joins on that platform are
probably failing too.

**First checks:**

1. `munshi_canary_consecutive_failures` shows the platform; `munshi_bot_joins_total{source="canary",outcome="failure"}`
   shows the failure `reason`.
2. Follow `andai/docs/runbooks/canaries.md` section 4: open the standing meeting yourself as a
   guest; fix its settings, or ship a bot-only fix (`bot-hotfix.md`) when the platform changed.

**Resolves** when the next canary on that platform succeeds.

## MunshiJoinSuccessLow

**Means:** over the last 24 hours fewer than 95% of counted bot joins on one platform reached
`recording`, with at least 5 counted joins (D-39: cancelled, denied and removed joins do not count)
(`service: bot-joins`, warning).

**First checks:**

1. `munshi_join_success_ratio_24h{source="all"}` names the platform; `munshi_bot_joins_total`
   by `reason` says why (`blocked_by_policy` and `ui_changed` point at the platform,
   `meeting_not_found` at the links users paste, `capacity_exhausted` at the bot cap,
   `internal_error` at munshi.ai).
2. Then as for MunshiCanaryFailing (`canaries.md`, `bot-hotfix.md`; `tenant-admission.md` when one
   customer's tenant holds the bot).

**Resolves** when the rate is back at 95% or fewer than 5 joins remain in the window.

## MunshiPipelineBacklog

**Means:** a pipeline stage has owed an item for more than 15 minutes, for at least a minute
(`service: pipeline`, warning). Stages: `dispatch` (scheduled meetings past their join time),
`ingest` (transcript copies), `minutes`, `recap` (mail), `deletion` (purges; past 900 s PORT-04 is
missed) and `intake` (invite mail).

**First checks:**

1. `munshi_pipeline_oldest_pending_seconds` and `munshi_pipeline_pending` by `stage` name the
   stage; `munshi_pipeline_failed_24h` shows whether items are also failing for good.
2. By stage: `dispatch` — the platform gateway and andai-worker's scheduled-dispatch log;
   `ingest` — meeting-api and the gateway; `minutes` — the language model route
   (`ANDAI_LLM_BASE_URL`; on a deployment without one, minutes stay owed by design: silence
   `stage` there or leave the route configured); `recap` — the SMTP relay
   (`munshi_mail_sent_total{kind="recap",outcome="failed"}`); `deletion` — meeting-api and object
   storage, see `andai/docs/erasure.md` for purges; `intake` — andai-intake's log and its
   database connection.
3. `docker compose -p <project> logs --tail=200 andai-worker` (logs carry ids and counts, never
   content).

**Resolves** when the stage's oldest item is under 15 minutes old.

## MunshiStorageHigh

**Means:** a watched filesystem has been more than 80% full for 10 minutes (`service: storage`,
critical). The watched mounts are a regex in the rule file (default: the host filesystems the node
exporter sees under `/host`, which hold Docker's data and with it the Postgres and object-storage
volumes); see `monitoring.md`.

**First checks:**

1. In Prometheus: `1 - node_filesystem_avail_bytes / node_filesystem_size_bytes` names the mount.
2. On the host: `df -h` and `docker system df`. `munshi_db_size_bytes` shows the andai database's
   share. Old images (`docker image prune` after a release), unpurged deleted meetings
   (MunshiPipelineBacklog on `deletion`) and Prometheus' own data (15 days) are the usual growth.
3. Grow the disk before it fills: Postgres stops accepting writes on a full disk.

**Resolves** when use is back at or under 80%.

## MunshiErasureSlaAtRisk

**Means:** a user erasure request has been open for more than 18 of its 24 hours, or a request
failed and needs the operator (`service: erasure`, critical; OPS-02, D-61).

**First checks:**

1. `munshi_erasures_pending`, `munshi_erasure_oldest_pending_seconds` and `munshi_erasures_failed`.
2. `make -C andai erase-status PROJECT=<project> ID=<request id>` (the members page's "Data
   erasure" list shows the ids) and `andai/docs/erasure.md`: a `purge_failed` request means the
   platform or object storage refused the purge.

**Resolves** when no request is older than 18 hours and none is failed.

## MunshiTargetDown

**Means:** Prometheus could not scrape a metrics endpoint for 5 minutes (`service`: `andai-core`,
`andai-worker`, `andai-intake` or `node-exporter`, critical). While it lasts, the alerts that read
that service are blind; andai-worker's endpoint carries every join, pipeline and erasure series.

**First checks:**

1. `docker compose -p <project> ps` — is the service running and healthy? `make -C andai health`.
2. `curl -s http://127.0.0.1:${ANDAI_WORKER_PORT:-18301}/metrics | head` — andai-worker answers
   503 without a database URL.
3. Prometheus' **Status → Targets** page shows the scrape error.

**Resolves** when the endpoint answers again.
