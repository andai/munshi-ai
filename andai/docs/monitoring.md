# Monitoring and alerts

munshi.ai ships an opt-in monitoring stack for Compose (OPS-05, D-62): Prometheus scrapes the
andai services and evaluates the munshi alert rules, Alertmanager routes alerts to the operator's
own channel (email, Slack or Microsoft Teams), and Perses shows the dashboards. Everything is
Apache-2.0, binds to 127.0.0.1 on the host only (D-15), and nothing sends an alert anywhere until
the operator turns a receiver on. The configs live in `andai/deploy/monitoring/`; what each alert
means is in `andai/docs/runbooks/alerts.md`; every series is in `andai/docs/metrics.md`.

## What runs

Under the `monitoring` compose profile (`andai/deploy/compose/docker-compose.andai.yml`):

| Service | Image (pinned by tag and digest, `andai/images.json`) | On the host | Does |
|---|---|---|---|
| `prometheus` | `prom/prometheus:v3.13.5` (3.13 LTS) | `127.0.0.1:${PROMETHEUS_PORT:-19090}` | Scrapes andai-core, andai-worker, andai-intake (`:8000/metrics`) and the node exporter every 30 s; evaluates the alert rules; keeps 15 days. |
| `alertmanager-config` | andai-worker | — | One-shot: renders Alertmanager's config from the settings below, then exits. |
| `alertmanager` | `prom/alertmanager:v0.34.2` | `127.0.0.1:${ALERTMANAGER_PORT:-19093}` | Groups alerts by name and service and sends them to the enabled receivers; silences. |
| `node-exporter` | `prom/node-exporter:v1.12.1` | — | Filesystem use for the storage alert. |
| `perses` | `persesdev/perses:v0.55.1` | `127.0.0.1:${PERSES_PORT:-19080}` | The dashboards (project `munshi`). |

Without the profile the stack is unchanged and andai-worker's own alert mail works as before.

## Turning it on

In the deployment's env file:

```sh
COMPOSE_PROFILES=dashboard,monitoring   # keep the profiles you already had
ANDAI_MONITORING=1
```

then `make -C andai up`. The first start pulls the four images above. `make -C andai ps` shows
`alertmanager-config` as exited 0; the others run.

## Receivers (all off by default)

With no receiver, Alertmanager routes every alert to a receiver named `none` that sends nothing,
and the alerts are visible only in Prometheus and Alertmanager on the host. Each receiver is turned
on separately:

| Receiver | Turn on with | Notes |
|---|---|---|
| Email | `ANDAI_ALERT_EMAIL=<operator address>` | Through the stack's relay (`SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_STARTTLS`), the same as andai's other mail. The password goes into a file only Alertmanager's user can read. |
| Slack | `ANDAI_ALERT_SLACK=1` **and** the file `slack_webhook_url` | A Slack incoming-webhook URL. |
| Microsoft Teams | `ANDAI_ALERT_TEAMS=1` **and** the file `teams_webhook_url` | A Teams **Workflows** webhook URL (the "Post to a channel when a webhook request is received" workflow). The old Office 365 connector webhooks are retired and are never configured. |

Webhook URLs are credentials. They live only in files in `ANDAI_ALERT_SECRETS_DIR` (default
`deploy/compose/secrets/alerting`, git-ignored), never in the env file, a compose file or a log.
The renderer only checks that a file exists; Alertmanager reads it when it sends. On a Linux host:

```sh
install -d -m 0750 -o root -g 65534 deploy/compose/secrets/alerting
install -m 0440 -o root -g 65534 /dev/stdin deploy/compose/secrets/alerting/slack_webhook_url   # paste the URL, then Ctrl-D
```

(uid/gid 65534 is Alertmanager's user.) A flag without its file, or a file without its flag, leaves
that receiver off; the renderer logs which.

After changing any of these, run `make -C andai up` (it re-runs `alertmanager-config`), then load
the new config into the running Alertmanager:

```sh
docker compose -p <project> restart alertmanager
```

`make -C andai logs S=alertmanager-config` shows `alertmanager config written: N receiver(s): …`.

### What a notification says

Every receiver uses the munshi templates (`andai/deploy/monitoring/alertmanager/templates/munshi.tmpl`)
and nothing else:

```
[FIRING] MunshiStorageHigh — storage
1 firing, 0 resolved
- critical: A storage filesystem is more than 80 percent full (firing)
```

Only the alert name, service, severity, the rule's fixed summary, the status and counts. Never a
meeting, org, person, address, link back to Alertmanager or any label value (D-62). One
notification when an alert group starts firing, a reminder every 4 hours while it fires, one when
it resolves.

### When andai-worker stops mailing

andai-worker has its own alert mail (two canary failures in a row, 24 h join success under 95%;
`runbooks/canaries.md`). With monitoring, Alertmanager raises the same conditions, so the worker
stands its mail down, but only while both hold:

- `ANDAI_MONITORING=1`, and
- the state file `alertmanager-config` wrote (`/run/munshi-alerting/state.json`, the
  `alerting-state` volume, read-only in andai-worker) names at least one enabled receiver.

With monitoring on and no receiver enabled (or no readable state file) the worker keeps mailing
`ANDAI_ALERT_EMAIL` and logs `monitoring on but no alert receiver enabled; worker alert mail stays
on`. It re-reads the state file on every alert pass (once a minute), so a re-render takes effect
without restarting it.

## Alerts

`andai/deploy/monitoring/prometheus/rules/munshi-alerts.yml`, each with a runbook entry in
`runbooks/alerts.md`:

| Alert | Fires when |
|---|---|
| `MunshiCanaryFailing` | 2 or more canary joins in a row failed on a platform |
| `MunshiJoinSuccessLow` | 24 h join success under 95% on a platform, with at least 5 counted joins |
| `MunshiPipelineBacklog` | a pipeline stage's oldest owed item is older than 15 minutes, for 1 minute |
| `MunshiStorageHigh` | a watched filesystem is more than 80% full for 10 minutes |
| `MunshiErasureSlaAtRisk` | a user erasure request is older than 18 hours, or one failed |
| `MunshiTargetDown` | a metrics endpoint (andai-core, andai-worker, andai-intake, node exporter) cannot be scraped for 5 minutes |

### Storage mounts

The node exporter reports its own root, which sits on the disk holding Docker's data (images, the
Postgres and object-storage volumes), and the host's filesystems read-only under `/host`
(`ANDAI_NODE_EXPORTER_HOST_ROOT`, default `/`). Container and kernel mounts are excluded.
`MunshiStorageHigh` watches the mount points matching `^/$|^/host(/.*)?$`; to watch other mounts,
edit that regex in both selectors of the rule and its unit test, then `make -C andai monitoring-check`.

## Dashboards

Open Perses at `http://127.0.0.1:${PERSES_PORT:-19080}`, project **munshi**. There is no sign-in:
reach it only from the host (for example over an SSH tunnel), never through a public ingress.
The dashboards are provisioned from `andai/deploy/monitoring/perses/provisioning` at start-up and
every hour, and overwrite edits made in the UI; change them in the repo.

| Dashboard | Shows |
|---|---|
| Bot joins | 24 h join success per platform, joins by outcome, failure reasons, sources, canary streaks and age |
| Speech-to-text latency | segment lag p50/p95 and segments without a lag (empty in practice today: `metrics.md`) |
| GPU | GPU utilisation and memory from NVIDIA dcgm-exporter |
| Pipeline | pending, oldest, failed and p50/p95 duration per stage; minutes, deletion and erasure backlogs; database size |
| Invite intake | SMTP verdicts, messages by status, outcome and quarantine reason |
| Email delivery | mail sent and failed by kind, failed share |

**The GPU dashboard shows no data on Compose, by design:** Compose runs no dcgm-exporter. GPU
metrics come from dcgm-exporter on the GPU nodes of a Kubernetes cluster; the panels fall back to 0
and say so when there are no GPU nodes.

## Checking the configs

```sh
uv run --directory andai pytest -q services/worker/tests/test_monitoring_rules.py \
  services/worker/tests/test_monitoring_dashboards.py services/worker/tests/test_alerting_config.py
make -C andai monitoring-check
```

The tests need no Docker. `monitoring-check` runs `promtool check config`, `promtool test rules`
(every alert at and just below its threshold), `amtool check-config` on a render with every
receiver off and one with every receiver on, and `percli lint` on the Perses resources, each in the
pinned image with no network.

## Not shipped

- **Grafana**: AGPL-3.0, so it is not in any image, chart or compose file (D-62). No Grafana JSON
  export of the dashboards is offered.
- Paging services (PagerDuty, Opsgenie), distributed tracing and log shipping.
