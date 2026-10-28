# andai/services/core/andai_core/db

The `andai` database layer: `models.py` (identity, tenancy and meetings schema; the org-less
system tables `canary_runs` and `alert_state`) and `session.py`
(`transaction()`, which sets the row-level-security context `app.org_id` / `app.user_id` /
`app.invite_token_hash` for one transaction only). The application connects as the `andai_app`
role, never as the owner.
