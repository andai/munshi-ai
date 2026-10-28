# andai/services/worker

The andai-worker service (FastAPI, Python 3.12): `Dockerfile`, `pyproject.toml`, the
`andai_worker` package and `tests/`. It runs andai-core's own code (workspace dependency
`andai-core`): `GET /healthz`, `GET /metrics` (join metrics in Prometheus text format), canary joins
into standing test meetings, and operator alert mail (`andai/docs/runbooks/canaries.md`).
