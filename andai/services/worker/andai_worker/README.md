# andai/services/worker/andai_worker

Python package of the andai-worker service:

- `main.py`: the app (`/healthz`, `/metrics`) and the background loops.
- `settings.py`: andai-core's settings plus the canary and alert settings.
- `ingest.py`: transcript ingest. It claims finished meetings through `andai_claim_ingest()` and
  stores andai's copy of each.
- `canary.py`: the canary scheduler (SKIP LOCKED claims, dispatch as the canary org, outcomes).
- `metrics.py`: the Prometheus collector over `andai_join_stats` and `canary_runs`.
- `alerts.py`: incident state and alert mail.
