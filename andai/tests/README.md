# andai/tests

Tests for the munshi.ai services under `andai/services/` (`test_skeletons.py`: the health-only
skeletons start and answer their health endpoints) and for the operator scripts under
`andai/scripts/` (`test_live_dispatch.py`: the live harness's env reading, redaction, lag and
frame handling; `test_env_example.py`: the root `.env.example` checker's failures and rewrite). Run with `uv run --directory andai pytest`.
