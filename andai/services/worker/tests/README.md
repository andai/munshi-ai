# andai/services/worker/tests

andai-worker tests (`test_worker_*`). `conftest.py` reuses andai-core's disposable Postgres and
Mailpit fixtures (`services/core/tests/conftest.py`); without Docker they skip, and
`ANDAI_REQUIRE_DOCKER=1` makes that a failure. `test_worker_canary_live.py` is marked `live` and
needs a running mock-bot stack (`ENV_FILE`). Run from the repo root:
`ANDAI_REQUIRE_DOCKER=1 uv run --directory andai pytest -q services/worker/tests`.
