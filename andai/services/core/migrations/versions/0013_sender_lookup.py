"""Sender lookup for the notetaker mailbox: who a From address is, across orgs, without lifting RLS.

andai-intake runs as `andai_app`, which sees memberships only inside one org's context. Before it
knows the org, it calls two functions instead (D-58):

* `andai_resolve_sender(email)` returns one row `(user_id, org_id, team_id)` per org membership of
  the active user whose `users.email` equals `lower(email)`. `team_id` is the user's team in that org
  when they are in exactly one, otherwise NULL. A disabled or invited-only user, or one with no
  membership, returns no rows. The caller decides what several rows mean.
* `andai_orgs_for_domain(domain)` returns the ids of the orgs whose `allowed_email_domains` contain
  the domain (case-insensitive).

Both are `SECURITY DEFINER`, owned by the NOLOGIN role `andai_scan`, have a fixed `search_path`, and
only `andai_app` may execute them (like 0003 and 0012). `andai_scan` gets column SELECT on exactly
what they read: `users (id, email, status)`, `org_memberships (org_id, user_id)`,
`team_memberships (team_id, org_id, user_id)` and `orgs.allowed_email_domains` (beside the 0005
`id`, `is_canary`). It also gets role-scoped read policies on the two membership tables; `orgs`
already has one (0005), and `users` has no row-level security.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "andai_app"
SCAN_ROLE = "andai_scan"

RESOLVE = "andai_resolve_sender(text)"
DOMAIN = "andai_orgs_for_domain(text)"
FUNCTIONS = (RESOLVE, DOMAIN)

# (table, columns) andai_scan may SELECT for these functions.
SCAN_SELECT = (
    ("users", ("id", "email", "status")),
    ("org_memberships", ("org_id", "user_id")),
    ("team_memberships", ("team_id", "org_id", "user_id")),
    ("orgs", ("allowed_email_domains",)),
)
# Row-level security is forced on these; `orgs` has had its scan_read policy since 0005.
SCAN_POLICY_TABLES = ("org_memberships", "team_memberships")


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
    for table, columns in SCAN_SELECT:
        op.execute(f"GRANT SELECT ({', '.join(columns)}) ON {table} TO {SCAN_ROLE}")
    for table in SCAN_POLICY_TABLES:
        op.execute(
            f"CREATE POLICY scan_read ON {table} AS PERMISSIVE FOR SELECT TO {SCAN_ROLE}"
            " USING (true)"
        )

    op.execute(
        f"""
        CREATE FUNCTION {RESOLVE}
        RETURNS TABLE (user_id uuid, org_id uuid, team_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          SELECT u.id, m.org_id,
                 (SELECT CASE WHEN count(*) = 1 THEN (array_agg(t.team_id))[1] END
                    FROM team_memberships t
                   WHERE t.org_id = m.org_id AND t.user_id = u.id)
            FROM users u
            JOIN org_memberships m ON m.user_id = u.id
           WHERE u.email = lower(btrim($1))
             AND u.status = 'active'
           ORDER BY m.org_id
        $$
        """
    )
    _hand_to_scan(RESOLVE)
    op.execute(
        f"""
        CREATE FUNCTION {DOMAIN}
        RETURNS SETOF uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          SELECT o.id
            FROM orgs o
           WHERE EXISTS (SELECT 1 FROM unnest(o.allowed_email_domains) AS d(domain)
                          WHERE lower(btrim(d.domain)) = lower(btrim($1)))
           ORDER BY o.id
        $$
        """
    )
    _hand_to_scan(DOMAIN)


def downgrade() -> None:
    for function in reversed(FUNCTIONS):
        op.execute(f"DROP FUNCTION {function}")
    for table in SCAN_POLICY_TABLES:
        op.execute(f"DROP POLICY scan_read ON {table}")
    for table, columns in reversed(SCAN_SELECT):
        op.execute(f"REVOKE SELECT ({', '.join(columns)}) ON {table} FROM {SCAN_ROLE}")
