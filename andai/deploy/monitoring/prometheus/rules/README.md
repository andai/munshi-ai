# andai/deploy/monitoring/prometheus/rules

`munshi-alerts.yml`: the munshi alert rules (closed `service`/`severity` labels, fixed summaries,
the operator-editable storage mount regex). Prometheus loads `*.yml` here. Each alert's meaning
and first checks: `andai/docs/runbooks/alerts.md`. Unit tests in `tests/`.
