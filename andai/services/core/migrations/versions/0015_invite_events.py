"""Scheduling from invites: `invite_events`, the meetings' scheduling columns, and the live-link
index without `scheduled` rows.

* `invite_events` holds one row per (org, normalised calendar UID, RECURRENCE-ID) andai-intake
  tracks: the event or series itself (`recurrence_id` NULL) and each instance sent on its own or
  as an override (`recurrence_id` = the instance's original start, UTC). `(org_id, uid_norm,
  recurrence_id)` is unique with NULLS NOT DISTINCT. A row keeps the SEQUENCE and DTSTAMP it was
  last changed by (RFC 5546 order), `state` (`active | cancelled`), the SUMMARY, ORGANIZER and join
  link, the member who asked (`requested_by`) and their team, `rrule` and the `timing` needed to
  expand the series again (start, end, wall-clock start and zone, RDATE/EXDATE, STATUS),
  `recurring`, the forward flags, and `last_inbound_id`: the last message applied, a plain uuid with
  no foreign key, so the 30-day `inbound_mail` purge never blocks on or cascades into events.
  Org-scoped like the other org tables: FORCE ROW LEVEL SECURITY with `org_isolation`, and SELECT,
  INSERT, UPDATE, DELETE for `andai_app`.
* `meetings` gains `scheduled_start` (UTC), `invite_event_id` (with `(invite_event_id, org_id)`
  referencing `invite_events (id, org_id)`, set NULL if the event goes), `occurrence_key` (the
  occurrence's original start: it does not change when the occurrence moves), `skipped_at` and
  `skipped_by`. `(invite_event_id, occurrence_key)` is unique: one meeting per occurrence.
  `ix_meetings_org_id_upcoming` reads the Upcoming section in scheduled-start order.
* `uq_meetings_live_join_url_key` (one live bot per link per org) no longer covers `scheduled`
  rows. A weekly invite holds one scheduled row per occurrence under the same link, and a link
  pasted while an invite for it is scheduled still dispatches; a scheduled meeting is checked
  against live rows again when it is dispatched. Duplicates among live rows are unchanged.
* `andai_invite_series_orgs()` lists the orgs that have an active recurring event, for
  andai-intake's hourly horizon top-up, which then reads each org's events as that org. It is
  `SECURITY DEFINER`, owned by the NOLOGIN role `andai_scan` (column grants on `org_id`, `state`
  and `recurring` only, through the role-scoped policy `scan_read`), executable by `andai_app`.

The downgrade cancels the scheduled meetings that came from invites (their scheduling columns go),
so the original live-link index can be built again.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "andai_app"
SCAN_ROLE = "andai_scan"
APP_ORG = "NULLIF(current_setting('app.org_id', true), '')::uuid"
FUNCTION = "andai_invite_series_orgs()"

STATES = ("active", "cancelled")
PLATFORMS = ("google_meet", "teams", "zoom")
TERMINAL = ("done", "failed", "cancelled")
LIVE_BEFORE = f"status NOT IN ({', '.join(repr(v) for v in TERMINAL)})"
LIVE_AFTER = f"status NOT IN ({', '.join(repr(v) for v in (*TERMINAL, 'scheduled'))})"
SCAN_SELECT = ("org_id", "state", "recurring")
MEETING_COLUMNS = (
    "scheduled_start",
    "invite_event_id",
    "occurrence_key",
    "skipped_at",
    "skipped_by",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ts(name: str, *, nullable: bool = True, now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if now else None,
    )


def _live_index(where: str) -> None:
    op.create_index(
        "uq_meetings_live_join_url_key",
        "meetings",
        ["org_id", "join_url_key"],
        unique=True,
        postgresql_where=sa.text(where),
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
        "invite_events",
        sa.Column(
            "id", pg.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("org_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("uid_norm", sa.Text, nullable=False),
        _ts("recurrence_id"),
        sa.Column("sequence", sa.Integer, nullable=False),
        _ts("dtstamp"),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("organizer", sa.Text, nullable=True),
        sa.Column("join_url", sa.Text, nullable=True),
        sa.Column("platform", sa.Text, nullable=True),
        sa.Column("requested_by", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("team_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("recurring", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("rrule", sa.Text, nullable=True),
        sa.Column(
            "timing", pg.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("forwarded_copy", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("auto_forwarded", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("last_inbound_id", pg.UUID(as_uuid=True), nullable=True),
        _ts("created_at", nullable=False, now=True),
        _ts("updated_at", nullable=False, now=True),
        sa.PrimaryKeyConstraint("id", name="pk_invite_events"),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], ondelete="CASCADE", name="fk_invite_events_org_id_orgs"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_invite_events_requested_by_users",
        ),
        sa.ForeignKeyConstraint(
            ["team_id", "org_id"],
            ["teams.id", "teams.org_id"],
            ondelete="SET NULL (team_id)",
            name="fk_invite_events_team_id_org_id_teams",
        ),
        sa.UniqueConstraint("id", "org_id", name="uq_invite_events_id_org_id"),
        sa.UniqueConstraint(
            "org_id",
            "uid_norm",
            "recurrence_id",
            name="uq_invite_events_org_id_uid_norm_recurrence_id",
            postgresql_nulls_not_distinct=True,
        ),
        sa.CheckConstraint(_in("state", STATES), name="ck_invite_events_state"),
        sa.CheckConstraint(
            "char_length(uid_norm) BETWEEN 1 AND 1024", name="ck_invite_events_uid_norm_length"
        ),
        sa.CheckConstraint(
            f"platform IS NULL OR {_in('platform', PLATFORMS)}", name="ck_invite_events_platform"
        ),
        sa.CheckConstraint("sequence >= 0", name="ck_invite_events_sequence"),
    )
    op.create_index(
        "ix_invite_events_active_series",
        "invite_events",
        ["org_id"],
        postgresql_where=sa.text("state = 'active' AND recurring"),
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON invite_events TO {APP_ROLE}")
    op.execute("ALTER TABLE invite_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE invite_events FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY org_isolation ON invite_events AS PERMISSIVE FOR ALL"
        f" USING (org_id = {APP_ORG}) WITH CHECK (org_id = {APP_ORG})"
    )

    # ── meetings: scheduling columns ──
    op.add_column("meetings", _ts("scheduled_start"))
    op.add_column("meetings", sa.Column("invite_event_id", pg.UUID(as_uuid=True), nullable=True))
    op.add_column("meetings", _ts("occurrence_key"))
    op.add_column("meetings", _ts("skipped_at"))
    op.add_column("meetings", sa.Column("skipped_by", pg.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_meetings_invite_event_id_org_id_invite_events",
        "meetings",
        "invite_events",
        ["invite_event_id", "org_id"],
        ["id", "org_id"],
        ondelete="SET NULL (invite_event_id)",
    )
    op.create_foreign_key(
        "fk_meetings_skipped_by_users",
        "meetings",
        "users",
        ["skipped_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_meetings_invite_event_id_occurrence_key",
        "meetings",
        ["invite_event_id", "occurrence_key"],
    )
    op.create_index(
        "ix_meetings_org_id_upcoming",
        "meetings",
        ["org_id", sa.text("(coalesce(scheduled_start, created_at))"), "id"],
        postgresql_where=sa.text("status = 'scheduled'"),
    )
    op.drop_index("uq_meetings_live_join_url_key", table_name="meetings")
    _live_index(LIVE_AFTER)

    # ── the horizon top-up's org list ──
    op.execute(f"GRANT SELECT ({', '.join(SCAN_SELECT)}) ON invite_events TO {SCAN_ROLE}")
    op.execute(
        f"CREATE POLICY scan_read ON invite_events AS PERMISSIVE FOR SELECT TO {SCAN_ROLE}"
        " USING (true)"
    )
    op.execute(
        f"""
        CREATE FUNCTION {FUNCTION} RETURNS SETOF uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$ SELECT DISTINCT org_id FROM invite_events WHERE state = 'active' AND recurring $$
        """
    )
    op.execute(f"GRANT CREATE ON SCHEMA public TO {SCAN_ROLE}")
    op.execute(f"ALTER FUNCTION {FUNCTION} OWNER TO {SCAN_ROLE}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {SCAN_ROLE}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {FUNCTION}")
    op.execute("DROP POLICY scan_read ON invite_events")
    op.execute(f"REVOKE SELECT ({', '.join(SCAN_SELECT)}) ON invite_events FROM {SCAN_ROLE}")

    # Scheduled meetings from invites lose their scheduling: cancel them, so the original index
    # (which covers `scheduled`) can hold. FORCE ROW LEVEL SECURITY binds the owner too; it is
    # lifted for this one statement, inside the migration's transaction.
    op.execute("ALTER TABLE meetings NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE meetings SET status = 'cancelled', end_reason = 'invite_cancelled',"
        " ended_at = coalesce(ended_at, now()), updated_at = now()"
        " WHERE status = 'scheduled' AND invite_event_id IS NOT NULL"
    )
    op.execute("ALTER TABLE meetings FORCE ROW LEVEL SECURITY")

    op.drop_index("uq_meetings_live_join_url_key", table_name="meetings")
    _live_index(LIVE_BEFORE)
    op.drop_index("ix_meetings_org_id_upcoming", table_name="meetings")
    op.drop_constraint("uq_meetings_invite_event_id_occurrence_key", "meetings", type_="unique")
    op.drop_constraint("fk_meetings_skipped_by_users", "meetings", type_="foreignkey")
    op.drop_constraint(
        "fk_meetings_invite_event_id_org_id_invite_events", "meetings", type_="foreignkey"
    )
    for column in reversed(MEETING_COLUMNS):
        op.drop_column("meetings", column)
    op.drop_table("invite_events")
