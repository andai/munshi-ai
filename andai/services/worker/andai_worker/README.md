# andai/services/worker/andai_worker

Python package of the andai-worker service:

- `main.py`: the app (`/healthz`, `/metrics`) and the background loops.
- `settings.py`: andai-core's settings plus the canary and alert settings.
- `ingest.py`: transcript ingest. It claims finished meetings through `andai_claim_ingest()` and
  stores andai's copy of each.
- `minutes.py`: minutes of meeting. It claims finished meetings with a stored transcript through
  `andai_claim_mom()` and runs `meetings.minutes_job.run_one` for each (only when the LLM route is set).
- `recap.py`: recap mail. It claims due recaps through `andai_claim_recap()` and runs
  `meetings.recap.send_one` for each (the first minutes to the requester and the meeting's team).
- `erasure.py`: user erasure. It claims due erasure requests through `andai_claim_erasures()` and
  runs `erasure.runner.run` for each, under the request's audit org.
- `canary.py`: the canary scheduler (SKIP LOCKED claims, dispatch as the canary org, outcomes).
- `metrics.py`: the Prometheus collectors over `andai_join_stats`, `canary_runs`,
  `andai_deletion_backlog`, `andai_mom_stats` and `andai_erasure_backlog`, among others.
- `alerts.py`: incident state and alert mail.
