"""`bot_settings`: when the bot joins on its own (`bot.autoJoin`), per org and per team (D-57).

* One row per scope: the org's value (`team_id IS NULL`) and each team override. `auto_join` is
  `only_when_invited`, `all_with_link` or `off`; an org without a row uses `only_when_invited`,
  and a team without a row uses the org's value. `(org_id, team_id)` is unique with NULLS NOT
  DISTINCT, so an org has at most one org-level row.
* A team override belongs to a team of the same org: `(team_id, org_id)` references
  `teams (id, org_id)` and goes with the team; every row goes with its org. `updated_by` is the
  member who last changed it (NULL once that user is deleted).
* Org-scoped like the other org tables: FORCE ROW LEVEL SECURITY with the `org_isolation` policy on
  `app.org_id`, and SELECT, INSERT, UPDATE, DELETE for `andai_app`.

Phase 8 generalises this into the settings store; until then it holds this one setting.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "andai_app"
APP_ORG = "NULLIF(current_setting('app.org_id', true), '')::uuid"
AUTO_JOIN_MODES = ("only_when_invited", "all_with_link", "off")


def upgrade() -> None:
    op.create_table(
        "bot_settings",
        sa.Column(
            "id", pg.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("org_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("team_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("auto_join", sa.Text, nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("updated_by", pg.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_bot_settings"),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], ondelete="CASCADE", name="fk_bot_settings_org_id_orgs"
        ),
        sa.ForeignKeyConstraint(
            ["team_id", "org_id"],
            ["teams.id", "teams.org_id"],
            ondelete="CASCADE",
            name="fk_bot_settings_team_id_org_id_teams",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            ondelete="SET NULL",
            name="fk_bot_settings_updated_by_users",
        ),
        sa.UniqueConstraint(
            "org_id",
            "team_id",
            name="uq_bot_settings_org_id_team_id",
            postgresql_nulls_not_distinct=True,
        ),
        sa.CheckConstraint(
            f"auto_join IN ({', '.join(repr(v) for v in AUTO_JOIN_MODES)})",
            name="ck_bot_settings_auto_join",
        ),
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON bot_settings TO {APP_ROLE}")
    op.execute("ALTER TABLE bot_settings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE bot_settings FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY org_isolation ON bot_settings AS PERMISSIVE FOR ALL"
        f" USING (org_id = {APP_ORG}) WITH CHECK (org_id = {APP_ORG})"
    )


def downgrade() -> None:
    op.drop_table("bot_settings")
