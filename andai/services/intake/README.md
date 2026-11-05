# andai/services/intake

The andai-intake service (FastAPI + aiosmtpd, Python 3.12): `Dockerfile`, `pyproject.toml`, the
`andai_intake` package and its `tests`. It is the notetaker mailbox's receiving end (INV-01, D-4):
an SMTP receiver that accepts mail for the configured notetaker addresses only and stores each
message durably in the andai database (`inbound_mail`, migration 0012) before answering 250; a
processor that turns verified members' invites into `scheduled` meetings (and keeps them in step
with updates, cancellations and recurrences, `invite_events`, migration 0015) and mails the
verified sender a plain-text confirmation (INV-04); plus `GET /healthz` and the 7/30-day retention
purge (D-58). andai-worker sends the bot when a scheduled meeting is due.

Captured mail is fed in with the replay client (D-54):

    uv run --directory andai python -m andai_intake.replay --host 127.0.0.1 --port 18325 \
        --mail-from organiser@example.test --rcpt notetaker@munshi.test invite.eml

Tests: `ANDAI_REQUIRE_DOCKER=1 uv run --directory andai pytest -q services/intake/tests` (a
disposable Postgres; `tests/conftest.py` re-exports andai-core's fixtures).
