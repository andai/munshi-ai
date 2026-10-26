# andai/tests

Tests for the munshi.ai services under `andai/services/` (`test_skeletons.py`: the health-only
skeletons start and answer their health endpoints) and for the operator scripts under
`andai/scripts/` (`test_live_dispatch.py`: the live harness's env reading, redaction, lag and
frame handling). Run with `uv run --directory andai pytest`.
