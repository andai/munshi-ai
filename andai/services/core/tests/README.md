# andai/services/core/tests

andai-core tests. `conftest.py` starts disposable Postgres, Valkey and Mailpit containers (random
loopback ports, removed afterwards) when a test needs them. Without Docker those tests skip;
set `ANDAI_REQUIRE_DOCKER=1` to make that a failure. Run from the repo root:
`ANDAI_REQUIRE_DOCKER=1 uv run --directory andai pytest -q`.
