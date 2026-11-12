# andai/deploy/monitoring

Monitoring as code for munshi.ai (OPS-05, D-62): what the Compose `monitoring` profile mounts into
Prometheus, Alertmanager and Perses. How to turn it on,
route alerts and read the dashboards: `andai/docs/monitoring.md`; what each alert means:
`andai/docs/runbooks/alerts.md`.

| Path | What it is |
|---|---|
| `prometheus/prometheus.yml` | Scrape jobs (andai-core, andai-worker, andai-intake on `:8000/metrics`; the node exporter), rule files, Alertmanager. |
| `prometheus/rules/munshi-alerts.yml` | The alert rules, with the operator-editable storage mount regex. |
| `prometheus/rules/tests/munshi-alerts.test.yml` | `promtool test rules` unit tests: each alert at and just below its threshold. |
| `alertmanager/templates/munshi.tmpl` | The only notification text: alert name, service, severity, fixed summary, status, counts. |
| `perses/config.yaml` | Perses server settings (no sign-in, 127.0.0.1 only, provisioning, datasource proxy limited to Prometheus). |
| `perses/provisioning/` | Project `munshi`, the Prometheus global datasource and six dashboards (joins, ASR, GPU, pipeline, intake, email). |

Alertmanager's own `alertmanager.yml` is not here: `python -m andai_worker.alerting_config`
renders it from the operator's settings when the stack starts (every receiver off by default).
No Grafana ships with munshi.ai (AGPL-3.0).

Checks: `uv run --directory andai pytest -q services/worker/tests/test_monitoring_rules.py
services/worker/tests/test_monitoring_dashboards.py services/worker/tests/test_alerting_config.py`
(no Docker) and `make -C andai monitoring-check` (promtool, amtool and percli in the pinned images).
