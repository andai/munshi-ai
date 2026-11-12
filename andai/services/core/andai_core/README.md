# andai/services/core/andai_core

Python package of the andai-core service: `main.py` (the FastAPI app: `/healthz` and the
`/api/v1` auth, org and meeting routes, the platform's signed webhook `/internal/platform-events`,
and the status reconciler; refuses to start outside dev
without a KEK), `settings.py`
(environment settings), `auth/` (sessions, login, two-factor login and its stage gate, role
checks, password reset, invites, bootstrap), `org/` (creating an org, members, invites, roles and
teams; org security settings: require 2FA, reset a member's 2FA), `platform/` (each org's
principal on the meeting platform, provisioned through its admin-api, key sealed under the KEK;
the gateway client that dispatches and reads bots with that key), `meetings/` (paste-link
dispatch, meeting status and typed failure reasons, the consent record, the reconciler, the
transcript and the live status and transcript relay; the minutes job), `testing/` (the fake LLM
route tests and the e2e compose profile use; never started by the default stack),
`llm/` (the minutes language model: one env-configured OpenAI-compatible route, schema-constrained
calls with typed errors, and a live probe), `minutes/` (minutes templates, prompts, transcript
packing, draft generation with map-reduce, and the owner and due-date rules),
`erasure/` (user erasure: the registry of user-referencing columns, the admin's request and the
step machine andai-worker drives), `mail.py` (settings-only SMTP), `__main__.py` (operator CLI:
`python -m andai_core bootstrap | principals-sync | canary-setup | kek-rewrap | kek-status |
erase-user | erase-status`),
`db/` (schema and RLS-scoped transactions), `crypto.py` (envelope encryption of stored secrets
under the KEK; active and retired KEKs, re-wrap), `sealed.py` (every sealed column, and the key
rotation's re-wrap job and counts per KEK), `audit.py` (append-only audit events) and `audit_actions.py` (the registry
of audit actions; `python -m andai_core.audit_actions --markdown` prints `andai/docs/audit-actions.md`).
