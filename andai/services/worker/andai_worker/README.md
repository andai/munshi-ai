# andai/services/worker/andai_worker

Python package of the andai-worker service: `main.py` (the app: `/healthz`, `/metrics`, and the
background loops), `settings.py` (andai-core's settings plus canary and alert settings),
`canary.py` (scheduler: SKIP LOCKED claims, dispatch as the canary org, outcomes), `metrics.py`
(Prometheus collector over `andai_join_stats` and `canary_runs`) and `alerts.py` (incident state
and mail).
