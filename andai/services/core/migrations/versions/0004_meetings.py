"""Meetings and consent records, org-scoped under row-level security, and the open-meeting org list.

* `meetings` — one row per bot andai-core sends (or tries to send) to a meeting: the pasted link,
  the platform's own platform / native id / meeting id as `POST /bots` returned them, the andai
  status with its typed failure reason, the platform's end reason, and the settings snapshot frozen
  at dispatch. Two partial unique indexes keep one live bot per meeting in an org: on
  `(org_id, join_url_key)` (the pasted link's host and path, lower-cased, query dropped) and on
  `(org_id, platform, native_meeting_id)`, both for rows that are not terminal. A `failed` row
  always carries a failure reason and no other row does.
* `consent_records` — one per meeting the bot was admitted to: the name it joined under, the notice
  text and privacy link, how the notice reached the room (`chat`, `bot_name_only`, `tts`) and when,
  and when a participant removed the bot.

Both get FORCE ROW LEVEL SECURITY with the Phase 3 policy (`org_id = app.org_id`) and CRUD grants
to `andai_app`.

`andai_open_meeting_orgs()` lists the org ids that have a meeting that is not terminal, for the
status reconciler, which runs as `andai_app` with no org context. Like `andai_principal_org_ids()`
(0003) it is `SECURITY DEFINER`, owned by the NOLOGIN role `andai_scan`, which may read only the
`org_id` and `status` columns of `meetings` through the role-scoped policy `scan_read`; only
`andai_app` may execute it. The reconciler then works one org at a time with that org's
`app.org_id`.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "andai_app"
SCAN_ROLE = "andai_scan"
APP_ORG = "NULLIF(current_setting('app.org_id', true), '')::uuid"
FUNCTION = "andai_open_meeting_orgs()"
TABLES = ("meetings", "consent_records")

PLATFORMS = ("google_meet", "teams", "zoom")
SOURCES = ("portal_link", "invite_mail", "teams_app", "meet_addon", "ics_sync", "canary")
STATUSES = (
    "scheduled",
    "dispatching",
    "awaiting_admission",
    "recording",
    "processing",
    "done",
    "failed",
    "cancelled",
)
TERMINAL = ("done", "failed", "cancelled")
FAILURE_REASONS = (
    "admission_denied",
    "admission_timeout",
    "meeting_not_found",
    "meeting_ended",
    "auth_required",
    "blocked_by_policy",
    "removed_by_participant",
    "ui_changed",
    "capacity_exhausted",
    "internal_error",
)
CHANNELS = ("chat", "bot_name_only", "tts")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


NOT_TERMINAL = f"status NOT IN ({', '.join(repr(v) for v in TERMINAL)})"


def _ts(name: str, *, nullable: bool = True, now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if now else None,
    )


def upgrade() -> None:
    exists = op.get_bind().execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": SCAN_ROLE}
    )
    if exists.first() is None:
        raise RuntimeError(
            f"role {SCAN_ROLE} does not exist; run db-init.sql first (make -C andai db-init)"
        )

    op.create_table(
        "meetings",
        sa.Column(
            "id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("org_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("team_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_by", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.Text, nullable=False, server_default=sa.text("'portal_link'")),
        sa.Column("platform", sa.Text, nullable=False),
        sa.Column("native_meeting_id", sa.Text, nullable=True),
        sa.Column("join_url", sa.Text, nullable=False),
        sa.Column("join_url_key", sa.Text, nullable=False),
        sa.Column("platform_meeting_id", sa.BigInteger, nullable=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("failure_reason", sa.Text, nullable=True),
        sa.Column("end_reason", sa.Text, nullable=True),
        sa.Column(
            "settings_snapshot",
            pg.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        _ts("started_at"),
        _ts("ended_at"),
        _ts("created_at", nullable=False, now=True),
        _ts("updated_at", nullable=False, now=True),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], ondelete="CASCADE", name="fk_meetings_org_id_orgs"
        ),
        sa.ForeignKeyConstraint(
            ["team_id", "org_id"],
            ["teams.id", "teams.org_id"],
            ondelete="SET NULL (team_id)",
            name="fk_meetings_team_id_org_id_teams",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_meetings_requested_by_users",
        ),
        sa.UniqueConstraint("id", "org_id", name="uq_meetings_id_org_id"),
        sa.CheckConstraint(_in("platform", PLATFORMS), name="ck_meetings_platform"),
        sa.CheckConstraint(_in("source", SOURCES), name="ck_meetings_source"),
        sa.CheckConstraint(_in("status", STATUSES), name="ck_meetings_status"),
        sa.CheckConstraint(
            f"failure_reason IS NULL OR {_in('failure_reason', FAILURE_REASONS)}",
            name="ck_meetings_failure_reason",
        ),
        sa.CheckConstraint(
            "(status = 'failed') = (failure_reason IS NOT NULL)",
            name="ck_meetings_failed_has_reason",
        ),
    )
    op.create_index("ix_meetings_org_id_created_at", "meetings", ["org_id", "created_at"])
    op.create_index(
        "ix_meetings_org_id_platform_meeting_id", "meetings", ["org_id", "platform_meeting_id"]
    )
    op.create_index("ix_meetings_org_id_join_url_key", "meetings", ["org_id", "join_url_key"])
    op.create_index(
        "uq_meetings_live_join_url_key",
        "meetings",
        ["org_id", "join_url_key"],
        unique=True,
        postgresql_where=sa.text(NOT_TERMINAL),
    )
    op.create_index(
        "uq_meetings_live_native_id",
        "meetings",
        ["org_id", "platform", "native_meeting_id"],
        unique=True,
        postgresql_where=sa.text(f"native_meeting_id IS NOT NULL AND {NOT_TERMINAL}"),
    )

    op.create_table(
        "consent_records",
        sa.Column("meeting_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("bot_display_name", sa.Text, nullable=False),
        sa.Column("disclosure_text", sa.Text, nullable=False),
        sa.Column("privacy_notice_url", sa.Text, nullable=False),
        sa.Column("disclosure_channel", sa.Text, nullable=False),
        sa.Column("disclosure_detail", sa.Text, nullable=True),
        _ts("disclosure_posted_at"),
        _ts("admitted_at", nullable=False),
        _ts("removed_at"),
        sa.Column("removed_by", sa.Text, nullable=True),
        _ts("created_at", nullable=False, now=True),
        _ts("updated_at", nullable=False, now=True),
        sa.ForeignKeyConstraint(
            ["meeting_id", "org_id"],
            ["meetings.id", "meetings.org_id"],
            ondelete="CASCADE",
            name="fk_consent_records_meeting_id_org_id_meetings",
        ),
        sa.CheckConstraint(
            _in("disclosure_channel", CHANNELS), name="ck_consent_records_disclosure_channel"
        ),
    )
    op.create_index("ix_consent_records_org_id", "consent_records", ["org_id"])

    for table in TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY org_isolation ON {table} AS PERMISSIVE FOR ALL"
            f" USING (org_id = {APP_ORG}) WITH CHECK (org_id = {APP_ORG})"
        )

    op.execute(f"GRANT SELECT (org_id, status) ON meetings TO {SCAN_ROLE}")
    op.execute(
        f"CREATE POLICY scan_read ON meetings AS PERMISSIVE FOR SELECT TO {SCAN_ROLE} USING (true)"
    )
    op.execute(
        f"""
        CREATE FUNCTION {FUNCTION} RETURNS SETOF uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$ SELECT DISTINCT org_id FROM meetings WHERE {NOT_TERMINAL} $$
        """
    )
    op.execute(f"GRANT CREATE ON SCHEMA public TO {SCAN_ROLE}")
    op.execute(f"ALTER FUNCTION {FUNCTION} OWNER TO {SCAN_ROLE}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {SCAN_ROLE}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {FUNCTION}")
    op.execute("DROP POLICY scan_read ON meetings")
    op.execute(f"REVOKE SELECT (org_id, status) ON meetings FROM {SCAN_ROLE}")
    op.drop_table("consent_records")
    op.drop_table("meetings")
