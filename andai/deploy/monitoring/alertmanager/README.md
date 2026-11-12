# andai/deploy/monitoring/alertmanager

Alertmanager's notification templates (`templates/`). Its `alertmanager.yml` is rendered at start-up
by `python -m andai_worker.alerting_config` (every receiver off by default; `andai/docs/monitoring.md`).
