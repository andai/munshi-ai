# andai/services/core/tests

andai-core tests. `conftest.py` starts disposable Postgres, Valkey and Mailpit containers (random
loopback ports, removed afterwards) when a test needs them. Without Docker those tests skip;
set `ANDAI_REQUIRE_DOCKER=1` to make that a failure. Run from the repo root:
`ANDAI_REQUIRE_DOCKER=1 uv run --directory andai pytest -q`.
`mom_gold/` holds the minutes gold set (README inside); `test_mom_gold.py` checks it offline and
`test_mom_gold_live.py` (`-m live`, `ENV_FILE`) runs it through the configured LLM route.
