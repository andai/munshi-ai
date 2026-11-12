# andai/deploy/monitoring/perses/provisioning/dashboards

Six dashboards: `joins`, `asr`, `gpu` (no data without GPU nodes), `pipeline`, `intake`, `email`.
Edit them here, not in the UI; `services/worker/tests/test_monitoring_dashboards.py` checks them and
`make -C andai monitoring-check` lints them against the pinned Perses.
