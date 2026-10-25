# andai/services/core/migrations/versions

One file per schema revision, applied in order by Alembic. Every new table needs its grants to
`andai_app` and, when it is org-scoped, row-level-security policies on `org_id`.
