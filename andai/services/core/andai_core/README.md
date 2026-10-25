# andai/services/core/andai_core

Python package of the andai-core service: `main.py` (the FastAPI app: `/healthz` and the
`/api/v1` auth routes; refuses to start outside dev without a KEK), `settings.py` (environment
settings), `auth/` (sessions, login, password reset, invites, bootstrap), `mail.py`
(settings-only SMTP), `__main__.py` (operator CLI: `python -m andai_core bootstrap`), `db/`
(schema and RLS-scoped transactions), `crypto.py` (envelope encryption of stored secrets under the
KEK) and `audit.py` (append-only audit events).
