# andai/services/core/andai_core

Python package of the andai-core service: `main.py` (the FastAPI app: `/healthz` and the
`/api/v1` auth, org and meeting routes, and the status reconciler; refuses to start outside dev
without a KEK), `settings.py`
(environment settings), `auth/` (sessions, login, two-factor login and its stage gate, role
checks, password reset, invites, bootstrap), `org/` (creating an org, members, invites, roles and
teams; org security settings: require 2FA, reset a member's 2FA), `platform/` (each org's
principal on the meeting platform, provisioned through its admin-api, key sealed under the KEK;
the gateway client that dispatches and reads bots with that key), `meetings/` (paste-link
dispatch, meeting status and typed failure reasons, the consent record, the reconciler, the
transcript and the live status and transcript relay),
`mail.py` (settings-only SMTP), `__main__.py` (operator CLI: `python -m andai_core bootstrap | principals-sync | canary-setup`),
`db/` (schema and RLS-scoped transactions), `crypto.py` (envelope encryption of stored secrets
under the KEK) and `audit.py` (append-only audit events).
