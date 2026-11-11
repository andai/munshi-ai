# andai/scripts

Operator scripts: `gen-secrets.sh` (fills stack secrets in an env file, never rotates existing ones),
`health.sh` (HTTP and container health of the stack), `vm.sh` (sync and run commands on the dev
VM through git), `images.py` (builds, checks and pushes the image catalogue `andai/images.json`;
run through `make -C andai images | images-check | images-push`) and `live_dispatch.py` (sends
the real bot into a live test meeting and prints its status changes and transcript lag as
redacted JSON lines; run through `make -C andai live-dispatch`, see `andai/docs/live-test.md`)
`env_example.py` (keeps the repo-root `.env.example` listing every variable a container reads,
with generated `# used by:` lines; run through `make -C andai env-check | env-example`) and
`encryption_check.sh` (read-only: do a Compose project's Postgres and object-storage volumes sit on
encrypted storage; run through `make -C andai encryption-check PROJECT=…`, see
`andai/docs/encryption-at-rest.md`). `tests/` holds the shell tests of these scripts.
