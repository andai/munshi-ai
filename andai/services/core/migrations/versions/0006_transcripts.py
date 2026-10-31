"""andai's own copy of each finished meeting's transcript and attendees, and the cross-org claim.

* `transcript_segments` holds one row per segment of a meeting's final transcript:
  - `id` is andai's `segment_id` (`seq` orders the segments);
  - times are seconds from the meeting's start plus the absolute times;
  - `text_raw` is exactly what the platform returned;
  - `text_canonical` is its Roman Hinglish form (`andai_core.text.canonical`) and `script` is the
    raw text's script tag;
  - `search_text` is `fold(text_canonical)`, the form search indexes.

  The table is org-scoped under FORCE ROW LEVEL SECURITY with the usual `org_isolation` policy and
  CRUD grants to `andai_app`. It cascades with its meeting.
* `meetings` gains these columns:
  - `title` (nullable);
  - `attendees`, a list of `{name, source: invite|speaker}`;
  - the ingest state: `transcript_state` (`none | pending | stored | failed`),
    `transcript_attempts`, `transcript_next_at` and `transcript_at`.
* `andai_claim_ingest(max)` is how andai-worker, running as `andai_app` with no org context, finds
  finished meetings to copy. It returns `(org_id, meeting_id)` for at most `max` meetings that match
  all of these:
  - the meeting is terminal and has a platform meeting id;
  - its transcript state is `none` or `pending`;
  - it has had fewer than 8 attempts;
  - its `transcript_next_at` has passed.

  The meetings are taken `FOR UPDATE SKIP LOCKED`, so two workers never claim the same one. Each
  claimed meeting is marked `pending` and its `transcript_next_at` is pushed out by a lease.

  Like the 0003–0005 functions, it is `SECURITY DEFINER` and owned by the NOLOGIN role
  `andai_scan`. Unlike them, it writes, so `andai_scan` gets these privileges and nothing more:
  - SELECT on the meeting columns the function filters on (`id`, `platform_meeting_id` and the
    three ingest columns; `org_id` and `status` were granted by 0004);
  - UPDATE on `transcript_state`, `transcript_attempts` and `transcript_next_at` only;
  - the role-scoped UPDATE policy `scan_claim` (SELECT comes from 0004's `scan_read`).

  Only `andai_app` may execute the function. The app role's own RLS view is unchanged: the worker
  then ingests each meeting inside a transaction under that meeting's org.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "andai_app"
SCAN_ROLE = "andai_scan"
APP_ORG = "NULLIF(current_setting('app.org_id', true), '')::uuid"
FUNCTION = "andai_claim_ingest(integer)"

SCRIPTS = ("latn", "deva", "mixed", "other")
TRANSCRIPT_STATES = ("none", "pending", "stored", "failed")
TERMINAL = ("done", "failed", "cancelled")
MAX_ATTEMPTS = 8
LEASE = "2 minutes"
SCAN_SELECT = (
    "id",
    "platform_meeting_id",
    "transcript_state",
    "transcript_attempts",
    "transcript_next_at",
)
SCAN_UPDATE = ("transcript_state", "transcript_attempts", "transcript_next_at")
TERMINAL_IN = f"({', '.join(repr(v) for v in TERMINAL)})"
DUE = (
    f"status IN {TERMINAL_IN} AND platform_meeting_id IS NOT NULL"
    " AND transcript_state IN ('none', 'pending')"
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    exists = op.get_bind().execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": SCAN_ROLE}
    )
    if exists.first() is None:
        raise RuntimeError(
            f"role {SCAN_ROLE} does not exist; run db-init.sql first (make -C andai db-init)"
        )

    # ── meetings: title, attendees, ingest state ──
    op.add_column("meetings", sa.Column("title", sa.Text, nullable=True))
    op.add_column(
        "meetings",
        sa.Column("attendees", pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column(
        "meetings",
        sa.Column(
            "transcript_state", sa.Text, nullable=False, server_default=sa.text("'none'")
        ),
    )
    op.add_column(
        "meetings",
        sa.Column("transcript_attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "meetings", sa.Column("transcript_next_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "meetings", sa.Column("transcript_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        "ALTER TABLE meetings ADD CONSTRAINT ck_meetings_transcript_state"
        f" CHECK ({_in('transcript_state', TRANSCRIPT_STATES)})"
    )
    op.create_index(
        "ix_meetings_transcript_due",
        "meetings",
        ["transcript_next_at"],
        postgresql_where=sa.text(DUE),
    )

    # ── transcript segments ──
    op.create_table(
        "transcript_segments",
        sa.Column("meeting_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("id", sa.Text, nullable=False),
        sa.Column("org_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("start_s", sa.Float, nullable=True),
        sa.Column("end_s", sa.Float, nullable=True),
        sa.Column("absolute_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("absolute_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("speaker", sa.Text, nullable=False),
        sa.Column("text_raw", sa.Text, nullable=False),
        sa.Column("text_canonical", sa.Text, nullable=False),
        sa.Column("script", sa.Text, nullable=False),
        sa.Column("language", sa.Text, nullable=True),
        sa.Column("search_text", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("meeting_id", "id", name="pk_transcript_segments"),
        sa.ForeignKeyConstraint(
            ["meeting_id", "org_id"],
            ["meetings.id", "meetings.org_id"],
            ondelete="CASCADE",
            name="fk_transcript_segments_meeting_id_org_id_meetings",
        ),
        sa.CheckConstraint(_in("script", SCRIPTS), name="ck_transcript_segments_script"),
    )
    op.create_index(
        "ix_transcript_segments_org_id_meeting_id_seq",
        "transcript_segments",
        ["org_id", "meeting_id", "seq"],
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON transcript_segments TO {APP_ROLE}")
    op.execute("ALTER TABLE transcript_segments ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE transcript_segments FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY org_isolation ON transcript_segments AS PERMISSIVE FOR ALL"
        f" USING (org_id = {APP_ORG}) WITH CHECK (org_id = {APP_ORG})"
    )

    # ── the cross-org ingest claim ──
    op.execute(f"GRANT SELECT ({', '.join(SCAN_SELECT)}) ON meetings TO {SCAN_ROLE}")
    op.execute(f"GRANT UPDATE ({', '.join(SCAN_UPDATE)}) ON meetings TO {SCAN_ROLE}")
    op.execute(
        f"CREATE POLICY scan_claim ON meetings AS PERMISSIVE FOR UPDATE TO {SCAN_ROLE}"
        " USING (true) WITH CHECK (true)"
    )
    op.execute(
        f"""
        CREATE FUNCTION {FUNCTION}
        RETURNS TABLE (org_id uuid, meeting_id uuid)
        LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          WITH due AS (
            SELECT m.id
              FROM meetings m
             WHERE m.status IN {TERMINAL_IN}
               AND m.platform_meeting_id IS NOT NULL
               AND m.transcript_state IN ('none', 'pending')
               AND m.transcript_attempts < {MAX_ATTEMPTS}
               AND (m.transcript_next_at IS NULL OR m.transcript_next_at <= now())
             ORDER BY m.transcript_next_at NULLS FIRST, m.id
             LIMIT greatest($1, 0)
             FOR UPDATE SKIP LOCKED
          )
          UPDATE meetings AS t
             SET transcript_state = 'pending',
                 transcript_next_at = now() + interval '{LEASE}'
            FROM due
           WHERE t.id = due.id
          RETURNING t.org_id, t.id
        $$
        """
    )
    op.execute(f"GRANT CREATE ON SCHEMA public TO {SCAN_ROLE}")
    op.execute(f"ALTER FUNCTION {FUNCTION} OWNER TO {SCAN_ROLE}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {SCAN_ROLE}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {FUNCTION}")
    op.execute("DROP POLICY scan_claim ON meetings")
    op.execute(f"REVOKE UPDATE ({', '.join(SCAN_UPDATE)}) ON meetings FROM {SCAN_ROLE}")
    op.execute(f"REVOKE SELECT ({', '.join(SCAN_SELECT)}) ON meetings FROM {SCAN_ROLE}")
    op.drop_table("transcript_segments")
    op.drop_index("ix_meetings_transcript_due", table_name="meetings")
    op.execute("ALTER TABLE meetings DROP CONSTRAINT ck_meetings_transcript_state")
    for column in (
        "transcript_at",
        "transcript_next_at",
        "transcript_attempts",
        "transcript_state",
        "attendees",
        "title",
    ):
        op.drop_column("meetings", column)
