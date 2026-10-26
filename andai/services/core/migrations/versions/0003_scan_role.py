"""List the org ids that have a platform principal, across orgs, without lifting row-level security.

Operator jobs that touch every org (`python -m andai_core principals-sync`) run as `andai_app`, which
sees `platform_principals` only for the org in its transaction context. They call
`andai_principal_org_ids()` instead:

* the function is `SECURITY DEFINER`, owned by the NOLOGIN role `andai_scan` (created by
  db-init.sql), with a fixed `search_path`;
* `andai_scan` may read only the `org_id` column of `platform_principals`, through the role-scoped
  policy `scan_read`; it has no other table privilege;
* only `andai_app` may execute it.

The caller learns which org ids exist and nothing else; it then opens one transaction per org with
that org's `app.org_id` to read or change the org's own rows.

Handing the function to `andai_scan` needs CREATE on schema public for the new owner, so that
privilege is granted for the transfer and revoked straight after.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCAN_ROLE = "andai_scan"
APP_ROLE = "andai_app"
FUNCTION = "andai_principal_org_ids()"


def upgrade() -> None:
    exists = op.get_bind().execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": SCAN_ROLE}
    )
    if exists.first() is None:
        raise RuntimeError(
            f"role {SCAN_ROLE} does not exist; run db-init.sql first (make -C andai db-init)"
        )
    op.execute(f"GRANT SELECT (org_id) ON platform_principals TO {SCAN_ROLE}")
    op.execute(
        f"CREATE POLICY scan_read ON platform_principals AS PERMISSIVE FOR SELECT TO {SCAN_ROLE}"
        " USING (true)"
    )
    op.execute(
        f"""
        CREATE FUNCTION {FUNCTION} RETURNS SETOF uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$ SELECT org_id FROM platform_principals $$
        """
    )
    op.execute(f"GRANT CREATE ON SCHEMA public TO {SCAN_ROLE}")
    op.execute(f"ALTER FUNCTION {FUNCTION} OWNER TO {SCAN_ROLE}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {SCAN_ROLE}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {FUNCTION}")
    op.execute("DROP POLICY scan_read ON platform_principals")
    op.execute(f"REVOKE SELECT (org_id) ON platform_principals FROM {SCAN_ROLE}")
