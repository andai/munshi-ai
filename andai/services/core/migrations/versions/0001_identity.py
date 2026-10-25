"""Identity and tenancy tables, and the app role's grants.

The tables are owned by `andai_owner` (the role this migration runs as). The app role `andai_app`
gets SELECT/INSERT/UPDATE/DELETE on every table except `audit_events`, where it gets INSERT only,
so the audit trail is append-only for the application. Both roles are created outside migrations
(`make -C andai db-init`, `migrations/db-init.sql`).

Revision ID: 0001
Revises:
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "andai_app"

CRUD_TABLES = (
    "orgs",
    "users",
    "org_memberships",
    "teams",
    "team_memberships",
    "sessions",
    "user_totp",
    "recovery_codes",
    "invites",
    "password_resets",
    "platform_principals",
)
INSERT_ONLY_TABLES = ("audit_events",)


def _id() -> sa.Column:
    return sa.Column(
        "id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )


def _ts(name: str, *, nullable: bool = True, now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if now else None,
    )


def _uuid(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, pg.UUID(as_uuid=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "orgs",
        _id(),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("slug", sa.Text, nullable=False),
        sa.Column(
            "allowed_email_domains",
            pg.ARRAY(sa.Text),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("require_2fa", sa.Boolean, nullable=False, server_default=sa.text("false")),
        _ts("created_at", nullable=False, now=True),
        sa.UniqueConstraint("slug", name="uq_orgs_slug"),
    )

    op.create_table(
        "users",
        _id(),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'invited'")),
        sa.Column("password_hash", sa.Text),
        _ts("created_at", nullable=False, now=True),
        _ts("last_login_at"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.CheckConstraint("email = lower(email)", name="ck_users_email_lowercase"),
        sa.CheckConstraint(
            "status IN ('invited', 'active', 'disabled')", name="ck_users_status"
        ),
    )

    op.create_table(
        "org_memberships",
        _uuid("org_id"),
        _uuid("user_id"),
        sa.Column("role", sa.Text, nullable=False),
        _ts("created_at", nullable=False, now=True),
        sa.PrimaryKeyConstraint("org_id", "user_id", name="pk_org_memberships"),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], ondelete="CASCADE", name="fk_org_memberships_org_id_orgs"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE", name="fk_org_memberships_user_id_users"
        ),
        sa.CheckConstraint("role IN ('owner', 'admin', 'member')", name="ck_org_memberships_role"),
    )
    op.create_index("ix_org_memberships_user_id", "org_memberships", ["user_id"])

    op.create_table(
        "teams",
        _id(),
        _uuid("org_id"),
        sa.Column("name", sa.Text, nullable=False),
        _ts("created_at", nullable=False, now=True),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], ondelete="CASCADE", name="fk_teams_org_id_orgs"
        ),
        sa.UniqueConstraint("org_id", "name", name="uq_teams_org_id_name"),
        sa.UniqueConstraint("id", "org_id", name="uq_teams_id_org_id"),
    )

    op.create_table(
        "team_memberships",
        _uuid("team_id"),
        _uuid("org_id"),
        _uuid("user_id"),
        sa.Column("role", sa.Text, nullable=False),
        _ts("created_at", nullable=False, now=True),
        sa.PrimaryKeyConstraint("team_id", "user_id", name="pk_team_memberships"),
        sa.ForeignKeyConstraint(
            ["team_id", "org_id"],
            ["teams.id", "teams.org_id"],
            ondelete="CASCADE",
            name="fk_team_memberships_team_id_org_id_teams",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["org_memberships.org_id", "org_memberships.user_id"],
            ondelete="CASCADE",
            name="fk_team_memberships_org_id_user_id_org_memberships",
        ),
        sa.CheckConstraint("role IN ('lead', 'member')", name="ck_team_memberships_role"),
    )
    op.create_index("ix_team_memberships_org_id", "team_memberships", ["org_id"])

    op.create_table(
        "sessions",
        _id(),
        sa.Column("token_hash", sa.Text, nullable=False),
        _uuid("user_id"),
        _uuid("org_id"),
        sa.Column("stage", sa.Text, nullable=False),
        sa.Column("mfa_failures", sa.Integer, nullable=False, server_default=sa.text("0")),
        _ts("created_at", nullable=False, now=True),
        _ts("last_seen_at", nullable=False, now=True),
        _ts("expires_at", nullable=False),
        sa.Column("user_agent", sa.Text),
        sa.Column("ip", pg.INET),
        sa.UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE", name="fk_sessions_user_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], ondelete="CASCADE", name="fk_sessions_org_id_orgs"
        ),
        sa.CheckConstraint(
            "stage IN ('mfa_pending', 'enroll_required', 'full')", name="ck_sessions_stage"
        ),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    op.create_table(
        "user_totp",
        sa.Column("user_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("secret_ciphertext", sa.Text, nullable=False),
        sa.Column("key_id", sa.Text, nullable=False),
        _ts("confirmed_at"),
        sa.Column("last_used_step", sa.BigInteger),
        _ts("created_at", nullable=False, now=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE", name="fk_user_totp_user_id_users"
        ),
    )

    op.create_table(
        "recovery_codes",
        _id(),
        _uuid("user_id"),
        sa.Column("code_hash", sa.Text, nullable=False),
        _ts("used_at"),
        _ts("created_at", nullable=False, now=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE", name="fk_recovery_codes_user_id_users"
        ),
    )
    op.create_index("ix_recovery_codes_user_id", "recovery_codes", ["user_id"])

    op.create_table(
        "invites",
        _id(),
        _uuid("org_id"),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("token_hash", sa.Text, nullable=False),
        _uuid("invited_by", nullable=True),
        _ts("created_at", nullable=False, now=True),
        _ts("expires_at", nullable=False),
        _ts("accepted_at"),
        sa.UniqueConstraint("token_hash", name="uq_invites_token_hash"),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], ondelete="CASCADE", name="fk_invites_org_id_orgs"
        ),
        sa.ForeignKeyConstraint(
            ["invited_by"], ["users.id"], ondelete="SET NULL", name="fk_invites_invited_by_users"
        ),
        sa.CheckConstraint("email = lower(email)", name="ck_invites_email_lowercase"),
        sa.CheckConstraint("role IN ('owner', 'admin', 'member')", name="ck_invites_role"),
    )
    op.create_index("ix_invites_org_id", "invites", ["org_id"])

    op.create_table(
        "password_resets",
        _id(),
        _uuid("user_id"),
        sa.Column("token_hash", sa.Text, nullable=False),
        _ts("created_at", nullable=False, now=True),
        _ts("expires_at", nullable=False),
        _ts("used_at"),
        sa.UniqueConstraint("token_hash", name="uq_password_resets_token_hash"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE", name="fk_password_resets_user_id_users"
        ),
    )
    op.create_index("ix_password_resets_user_id", "password_resets", ["user_id"])

    op.create_table(
        "platform_principals",
        sa.Column("org_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("platform_user_id", sa.BigInteger, nullable=False),
        sa.Column("key_ciphertext", sa.Text, nullable=False),
        sa.Column("key_id", sa.Text, nullable=False),
        _ts("created_at", nullable=False, now=True),
        _ts("rotated_at", nullable=False, now=True),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], ondelete="CASCADE", name="fk_platform_principals_org_id_orgs"
        ),
    )

    op.create_table(
        "audit_events",
        _id(),
        _uuid("org_id"),
        _uuid("actor_user_id", nullable=True),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("target", sa.Text, nullable=False),
        _ts("at", nullable=False, now=True),
        sa.Column("ip", pg.INET),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], ondelete="RESTRICT", name="fk_audit_events_org_id_orgs"
        ),
    )
    op.create_index("ix_audit_events_at", "audit_events", ["at"])
    op.create_index("ix_audit_events_org_id_at", "audit_events", ["org_id", "at"])

    for table in CRUD_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
    for table in INSERT_ONLY_TABLES:
        op.execute(f"GRANT INSERT ON {table} TO {APP_ROLE}")


def downgrade() -> None:
    for table in (
        "audit_events",
        "platform_principals",
        "password_resets",
        "invites",
        "recovery_codes",
        "user_totp",
        "sessions",
        "team_memberships",
        "teams",
        "org_memberships",
        "users",
        "orgs",
    ):
        op.drop_table(table)
