"""Row-level security on org_id for every org-scoped table.

Each org-scoped table gets ENABLE and FORCE ROW LEVEL SECURITY (FORCE so that even the owner role
is bound) and policies over the transaction-local settings that `andai_core.db.session` sets:

* `teams`, `team_memberships`, `invites`, `platform_principals`, `audit_events`:
  `org_id = app.org_id` for reads and writes.
* `orgs`: `id = app.org_id`.
* `org_memberships`: reads where `org_id = app.org_id` OR `user_id = app.user_id` (login lists a
  user's orgs before an org is chosen); writes only where `org_id = app.org_id`.
* `invites` also has a second, permissive SELECT policy `token_hash = app.invite_token_hash`, so
  invite acceptance can find exactly the presented invite before its org is known.

Unset settings read as NULL (`NULLIF(current_setting(name, true), '')`; a setting that was set in an
earlier transaction on the same connection reads as ''), so a query without context matches no
rows. Bootstrap needs no special path: the app generates the org id and sets `app.org_id` before
inserting the org and its owner membership. Global tables (users, sessions, password_resets,
user_totp, recovery_codes) have no RLS; code filters them by user id.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ORG = "NULLIF(current_setting('app.org_id', true), '')::uuid"
APP_USER = "NULLIF(current_setting('app.user_id', true), '')::uuid"
APP_INVITE_TOKEN_HASH = "NULLIF(current_setting('app.invite_token_hash', true), '')"

ORG_ID_TABLES = ("teams", "team_memberships", "invites", "platform_principals", "audit_events")
RLS_TABLES = ("orgs", "org_memberships", *ORG_ID_TABLES)

# (table, policy, command, USING, WITH CHECK)
POLICIES: tuple[tuple[str, str, str, str | None, str | None], ...] = (
    ("orgs", "org_isolation", "ALL", f"id = {APP_ORG}", f"id = {APP_ORG}"),
    *(
        (table, "org_isolation", "ALL", f"org_id = {APP_ORG}", f"org_id = {APP_ORG}")
        for table in ORG_ID_TABLES
    ),
    (
        "org_memberships",
        "org_or_own_read",
        "SELECT",
        f"org_id = {APP_ORG} OR user_id = {APP_USER}",
        None,
    ),
    ("org_memberships", "org_insert", "INSERT", None, f"org_id = {APP_ORG}"),
    ("org_memberships", "org_update", "UPDATE", f"org_id = {APP_ORG}", f"org_id = {APP_ORG}"),
    ("org_memberships", "org_delete", "DELETE", f"org_id = {APP_ORG}", None),
    ("invites", "token_lookup", "SELECT", f"token_hash = {APP_INVITE_TOKEN_HASH}", None),
)


def upgrade() -> None:
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    for table, name, command, using, check in POLICIES:
        sql = f"CREATE POLICY {name} ON {table} AS PERMISSIVE FOR {command}"
        if using is not None:
            sql += f" USING ({using})"
        if check is not None:
            sql += f" WITH CHECK ({check})"
        op.execute(sql)


def downgrade() -> None:
    for table, name, *_ in reversed(POLICIES):
        op.execute(f"DROP POLICY {name} ON {table}")
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
