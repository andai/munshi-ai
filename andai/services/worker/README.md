# andai/services/worker

The andai-worker service (FastAPI, Python 3.12): `Dockerfile`, `pyproject.toml`, the
`andai_worker` package and `tests/`. It runs andai-core's own code (workspace dependency
`andai-core`): `GET /healthz`, `GET /metrics` (join metrics in Prometheus text format), join-time
dispatch of meetings scheduled from invites (`andai_worker/scheduled.py`: claimed through
`andai_claim_scheduled` at start minus `ANDAI_INTAKE_JOIN_LEAD_S`, dispatched as their requester),
transcript ingest, minutes and their recap mail, meeting deletion, user erasure
(`andai_worker/erasure.py`, `andai/docs/erasure.md`), data exports and the expiry of their bundles
(`andai_worker/exports.py`), canary joins into standing test meetings,
and operator alert mail (`andai/docs/runbooks/canaries.md`).
