"""Meeting deletion: hide at once, purge in andai-worker, and keep D-38's removal window.

* `meetings` gains the deletion state:
  - `deleted_at` and `deleted_by`, set by `DELETE /api/v1/meetings/{id}` in the transaction that
    writes its `meeting.delete` audit event. Every read path treats a set `deleted_at` as "no such
    meeting" from then on;
  - `deletion_state` (`none | pending | failed`), `deletion_attempts` and `deletion_next_at`, the
    purge's retry state. A hidden meeting is `pending` until the worker has removed it from the
    platform and from andai (the row itself goes then), or `failed` once its attempts are spent.
    A meeting is hidden exactly when its state is not `none`.
* `meeting_removals` keeps one content-free marker per meeting participants removed the bot from
  (`org_id`, the link's `join_url_key`, `removed_at`), written by the purge when it deletes that
  meeting's consent record, so the 12-hour no-rejoin rule (D-38) outlives the meeting. Org-scoped
  under FORCE ROW LEVEL SECURITY; `andai_app` may SELECT and INSERT only.
* `andai_claim_deletions(max)` hands andai-worker, running as `andai_app` with no org context, at
  most `max` pending deletions that are due, as `(org_id, meeting_id)`. The rows are taken
  `FOR UPDATE SKIP LOCKED` and leased by pushing `deletion_next_at` out, so two workers never purge
  the same meeting at once.
* `andai_deletion_backlog()` answers the number of hidden meetings not yet purged (`pending` or
  `failed`) and the oldest `deleted_at` among them, for the worker's metrics. Nothing else leaves
  it.
* `andai_claim_ingest(max)` (0006) is replaced by the same claim that also skips hidden meetings.

The three functions are `SECURITY DEFINER`, owned by the NOLOGIN role `andai_scan` and executable by
`andai_app` only. `andai_scan` gets SELECT on the four deletion columns and UPDATE on
`deletion_next_at` alone (the lease); the `scan_claim` and `scan_read` policies of 0004/0006 cover
the rows.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "andai_app"
SCAN_ROLE = "andai_scan"
APP_ORG = "NULLIF(current_setting('app.org_id', true), '')::uuid"
CLAIM_DELETIONS = "andai_claim_deletions(integer)"
BACKLOG = "andai_deletion_backlog()"
CLAIM_INGEST = "andai_claim_ingest(integer)"

DELETION_STATES = ("none", "pending", "failed")
LEASE = "2 minutes"
SCAN_SELECT = ("deleted_at", "deletion_state", "deletion_attempts", "deletion_next_at")
SCAN_UPDATE = ("deletion_next_at",)

# andai_claim_ingest as 0006 created it, and with hidden meetings skipped.
INGEST_TERMINAL = "('done', 'failed', 'cancelled')"
INGEST_MAX_ATTEMPTS = 8
INGEST_LEASE = "2 minutes"


def _claim_ingest(extra: str) -> str:
    return f"""
        CREATE FUNCTION {CLAIM_INGEST}
        RETURNS TABLE (org_id uuid, meeting_id uuid)
        LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          WITH due AS (
            SELECT m.id
              FROM meetings m
             WHERE m.status IN {INGEST_TERMINAL}
               AND m.platform_meeting_id IS NOT NULL
               AND m.transcript_state IN ('none', 'pending')
               AND m.transcript_attempts < {INGEST_MAX_ATTEMPTS}
               AND (m.transcript_next_at IS NULL OR m.transcript_next_at <= now()){extra}
             ORDER BY m.transcript_next_at NULLS FIRST, m.id
             LIMIT greatest($1, 0)
             FOR UPDATE SKIP LOCKED
          )
          UPDATE meetings AS t
             SET transcript_state = 'pending',
                 transcript_next_at = now() + interval '{INGEST_LEASE}'
            FROM due
           WHERE t.id = due.id
          RETURNING t.org_id, t.id
        $$
        """


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

    # ── meetings: the deletion state ──
    op.add_column("meetings", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("meetings", sa.Column("deleted_by", pg.UUID(as_uuid=True), nullable=True))
    op.add_column(
        "meetings",
        sa.Column("deletion_state", sa.Text, nullable=False, server_default=sa.text("'none'")),
    )
    op.add_column(
        "meetings",
        sa.Column("deletion_attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "meetings", sa.Column("deletion_next_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_meetings_deleted_by_users", "meetings", "users", ["deleted_by"], ["id"],
        ondelete="SET NULL",
    )
    states = ", ".join(repr(v) for v in DELETION_STATES)
    op.execute(
        "ALTER TABLE meetings ADD CONSTRAINT ck_meetings_deletion_state"
        f" CHECK (deletion_state IN ({states}))"
    )
    op.execute(
        "ALTER TABLE meetings ADD CONSTRAINT ck_meetings_hidden_when_deleted"
        " CHECK ((deleted_at IS NULL) = (deletion_state = 'none'))"
    )
    op.create_index(
        "ix_meetings_deletion_due",
        "meetings",
        ["deletion_next_at"],
        postgresql_where=sa.text("deletion_state = 'pending'"),
    )

    # ── removal markers (D-38 across deletion) ──
    op.create_table(
        "meeting_removals",
        sa.Column(
            "id", pg.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("org_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("join_url_key", sa.Text, nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_meeting_removals"),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], ondelete="CASCADE", name="fk_meeting_removals_org_id_orgs"
        ),
    )
    op.create_index(
        "ix_meeting_removals_org_id_join_url_key_removed_at",
        "meeting_removals",
        ["org_id", "join_url_key", "removed_at"],
    )
    op.execute(f"GRANT SELECT, INSERT ON meeting_removals TO {APP_ROLE}")
    op.execute("ALTER TABLE meeting_removals ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE meeting_removals FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY org_isolation ON meeting_removals AS PERMISSIVE FOR ALL"
        f" USING (org_id = {APP_ORG}) WITH CHECK (org_id = {APP_ORG})"
    )

    # ── the cross-org deletion claim and backlog ──
    op.execute(f"GRANT SELECT ({', '.join(SCAN_SELECT)}) ON meetings TO {SCAN_ROLE}")
    op.execute(f"GRANT UPDATE ({', '.join(SCAN_UPDATE)}) ON meetings TO {SCAN_ROLE}")
    op.execute(
        f"""
        CREATE FUNCTION {CLAIM_DELETIONS}
        RETURNS TABLE (org_id uuid, meeting_id uuid)
        LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          WITH due AS (
            SELECT m.id
              FROM meetings m
             WHERE m.deletion_state = 'pending'
               AND (m.deletion_next_at IS NULL OR m.deletion_next_at <= now())
             ORDER BY m.deletion_next_at NULLS FIRST, m.id
             LIMIT greatest($1, 0)
             FOR UPDATE SKIP LOCKED
          )
          UPDATE meetings AS t
             SET deletion_next_at = now() + interval '{LEASE}'
            FROM due
           WHERE t.id = due.id
          RETURNING t.org_id, t.id
        $$
        """
    )
    _hand_to_scan(CLAIM_DELETIONS)
    op.execute(
        f"""
        CREATE FUNCTION {BACKLOG}
        RETURNS TABLE (pending bigint, oldest_deleted_at timestamptz)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          SELECT count(*)::bigint, min(m.deleted_at)
            FROM meetings m
           WHERE m.deletion_state IN ('pending', 'failed')
        $$
        """
    )
    _hand_to_scan(BACKLOG)

    # ── the ingest claim skips hidden meetings ──
    op.execute(f"DROP FUNCTION {CLAIM_INGEST}")
    op.execute(_claim_ingest("\n               AND m.deletion_state = 'none'"))
    _hand_to_scan(CLAIM_INGEST)


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {CLAIM_INGEST}")
    op.execute(_claim_ingest(""))
    _hand_to_scan(CLAIM_INGEST)

    op.execute(f"DROP FUNCTION {BACKLOG}")
    op.execute(f"DROP FUNCTION {CLAIM_DELETIONS}")
    op.execute(f"REVOKE UPDATE ({', '.join(SCAN_UPDATE)}) ON meetings FROM {SCAN_ROLE}")
    op.execute(f"REVOKE SELECT ({', '.join(SCAN_SELECT)}) ON meetings FROM {SCAN_ROLE}")
    op.drop_table("meeting_removals")
    op.drop_index("ix_meetings_deletion_due", table_name="meetings")
    op.execute("ALTER TABLE meetings DROP CONSTRAINT ck_meetings_hidden_when_deleted")
    op.execute("ALTER TABLE meetings DROP CONSTRAINT ck_meetings_deletion_state")
    op.drop_constraint("fk_meetings_deleted_by_users", "meetings", type_="foreignkey")
    for column in (
        "deletion_next_at", "deletion_attempts", "deletion_state", "deleted_by", "deleted_at"
    ):
        op.drop_column("meetings", column)
