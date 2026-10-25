# andai/services/core/andai_core

Python package of the andai-core service: `main.py` (the FastAPI app, `/healthz`), `settings.py`
(environment settings), `db/` (schema and RLS-scoped transactions), `crypto.py` (envelope
encryption of stored secrets under the KEK) and `audit.py` (append-only audit events).
