"""The audit trail is append-only for every role, and an org's owners and admins can read it.

* `andai_audit_append_only()` raises `audit_events is append-only` (SQLSTATE 42501,
  insufficient_privilege). Two triggers call it: `trg_audit_events_no_update_delete` (BEFORE UPDATE
  OR DELETE, per row) and `trg_audit_events_no_truncate` (BEFORE TRUNCATE, per statement; a row
  trigger never sees a TRUNCATE). They fire for the owner role and for a superuser too, so an audit
  row cannot be changed or removed once written. The app role still has INSERT only (0001).

  Claim limit: a superuser can still get round this by disabling the triggers (`ALTER TABLE ...
  DISABLE TRIGGER`, `session_replication_role = replica`) or dropping the table. Nothing in andai
  connects as a superuser; the guarantee is against the roles andai uses.

* `andai_audit_page(p_from, p_to, p_actions, p_before_at, p_before_id, p_limit)` returns one page
  of the org's events, newest first (`at`, then `id`): events with `p_from <= at < p_to`, whose
  action is in `p_actions` (all actions when NULL), after the keyset position `(p_before_at,
  p_before_id)` when given, at most `p_limit` rows clamped to 1..100. The org is never a parameter:
  the function reads the transaction's `app.org_id` itself, so a caller cannot ask for another
  org's trail, and without a context it returns nothing.

  `SECURITY DEFINER`, owned by the NOLOGIN role `andai_scan`, executable by `andai_app` only (like
  0016). `andai_scan` gets SELECT on `audit_events`, and its role-scoped policy `scan_read` repeats
  `org_id = app.org_id`, so the definer reads one org's rows under row-level security as well as
  the function's own filter. The app role still cannot SELECT the table itself.

* `ix_audit_events_org_id_at_id` on `(org_id, at DESC, id DESC)` serves the page.

The downgrade drops the function, policy, grant, index, triggers and trigger function: below this
revision audit rows are protected only by the app role's grants.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "andai_app"
SCAN_ROLE = "andai_scan"
APP_ORG = "NULLIF(current_setting('app.org_id', true), '')::uuid"
GUARD = "andai_audit_append_only()"
PAGE = "andai_audit_page(timestamptz, timestamptz, text[], timestamptz, uuid, integer)"
ROW_TRIGGER = "trg_audit_events_no_update_delete"
TRUNCATE_TRIGGER = "trg_audit_events_no_truncate"
INDEX = "ix_audit_events_org_id_at_id"
MAX_PAGE = 100


def _hand_to_scan(function: str) -> None:
    op.execute(f"GRANT CREATE ON SCHEMA public TO {SCAN_ROLE}")
    op.execute(f"ALTER FUNCTION {function} OWNER TO {SCAN_ROLE}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {SCAN_ROLE}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {function} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO {APP_ROLE}")


def upgrade() -> None:
    exists = op.get_bind().execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": SCAN_ROLE}
    )
    if exists.first() is None:
        raise RuntimeError(
            f"role {SCAN_ROLE} does not exist; run db-init.sql first (make -C andai db-init)"
        )

    op.execute(
        f"""
        CREATE FUNCTION {GUARD}
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = public, pg_temp
        AS $$
        BEGIN
          RAISE EXCEPTION 'audit_events is append-only'
            USING ERRCODE = 'insufficient_privilege';
        END
        $$
        """
    )
    op.execute(f"REVOKE EXECUTE ON FUNCTION {GUARD} FROM PUBLIC")
    op.execute(
        f"CREATE TRIGGER {ROW_TRIGGER} BEFORE UPDATE OR DELETE ON audit_events"
        f" FOR EACH ROW EXECUTE FUNCTION {GUARD}"
    )
    op.execute(
        f"CREATE TRIGGER {TRUNCATE_TRIGGER} BEFORE TRUNCATE ON audit_events"
        f" FOR EACH STATEMENT EXECUTE FUNCTION {GUARD}"
    )

    op.execute(f"CREATE INDEX {INDEX} ON audit_events (org_id, at DESC, id DESC)")
    op.execute(f"GRANT SELECT ON audit_events TO {SCAN_ROLE}")
    op.execute(
        f"CREATE POLICY scan_read ON audit_events AS PERMISSIVE FOR SELECT TO {SCAN_ROLE}"
        f" USING (org_id = {APP_ORG})"
    )
    op.execute(
        f"""
        CREATE FUNCTION {PAGE}
        RETURNS TABLE (
          id uuid, at timestamptz, actor_user_id uuid, action text, target text, ip inet
        )
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          SELECT e.id, e.at, e.actor_user_id, e.action, e.target, e.ip
            FROM audit_events e
           WHERE e.org_id = {APP_ORG}
             AND e.at >= $1
             AND e.at < $2
             AND ($3 IS NULL OR e.action = ANY($3))
             AND ($4 IS NULL OR $5 IS NULL OR (e.at, e.id) < ($4, $5))
           ORDER BY e.at DESC, e.id DESC
           LIMIT least(greatest(coalesce($6, {MAX_PAGE}), 1), {MAX_PAGE})
        $$
        """
    )
    _hand_to_scan(PAGE)


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {PAGE}")
    op.execute("DROP POLICY scan_read ON audit_events")
    op.execute(f"REVOKE SELECT ON audit_events FROM {SCAN_ROLE}")
    op.execute(f"DROP INDEX {INDEX}")
    op.execute(f"DROP TRIGGER {TRUNCATE_TRIGGER} ON audit_events")
    op.execute(f"DROP TRIGGER {ROW_TRIGGER} ON audit_events")
    op.execute(f"DROP FUNCTION {GUARD}")
