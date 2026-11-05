# andai/services/intake/tests

andai-intake tests. `conftest.py` re-exports andai-core's fixtures (`services/core/tests/conftest.py`):
a disposable Postgres initialised with `db-init.sql` and migrated to head. Without Docker those tests
skip; `ANDAI_REQUIRE_DOCKER=1` makes that a failure. `test_smtp_receive.py` runs the real aiosmtpd
receiver on an ephemeral loopback port with stdlib smtplib as the sender (recipient and size refusals,
store-then-250, 451 on a store failure, dedup, replay); `test_purge.py` covers the 7/30-day retention
and the store's 24-hour dedup. Run from the repo root:
`ANDAI_REQUIRE_DOCKER=1 uv run --directory andai pytest -q services/intake/tests`.
