# andai/scripts/tests

Shell tests of the operator scripts, run with stub tools first on PATH (no Docker, no real disks):
`test_encryption_check.sh` covers `encryption_check.sh`. Run one with
`bash andai/scripts/tests/test_encryption_check.sh`; the andai pytest suite runs it too
(`andai/tests/test_encryption_check.py`).
