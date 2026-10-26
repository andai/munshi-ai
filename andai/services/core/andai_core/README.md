# andai/services/core/andai_core

Python package of the andai-core service: `main.py` (the FastAPI app: `/healthz` and the
`/api/v1` auth and org routes; refuses to start outside dev without a KEK), `settings.py`
(environment settings), `auth/` (sessions, login, two-factor login and its stage gate, role
checks, password reset, invites, bootstrap), `org/` (members, invites, roles and teams; org security
settings: require 2FA, reset a member's 2FA), `mail.py` (settings-only SMTP), `__main__.py` (operator CLI: `python -m andai_core bootstrap`), `db/`
(schema and RLS-scoped transactions), `crypto.py` (envelope encryption of stored secrets under the
KEK) and `audit.py` (append-only audit events).
