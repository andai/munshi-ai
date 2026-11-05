"""Join-time dispatch of scheduled meetings: the claim andai-worker takes due meetings with.

* `meetings` gains `scheduled_end` (when the occurrence ends, UTC; andai-intake writes it with
  `scheduled_start`), `dispatch_attempts` (how often a scheduled meeting was claimed for dispatch)
  and `dispatch_lease_until` (no worker claims it again before then). `ix_meetings_scheduled_due`
  reads the scheduled, unskipped meetings in start order.
* `andai_claim_scheduled(max, lead_s, at)` hands andai-worker at most `max` meetings that are
  `scheduled`, not skipped, not deleted, have a `scheduled_start`, are due (`scheduled_start -
  lead_s <= at`) and have no live lease. Each is taken `FOR UPDATE SKIP LOCKED`, its attempts
  counted up and leased for 30 s × 2^(earlier attempts), at most 15 minutes, so two workers never
  take the same meeting, a worker that died is replaced once the lease runs out, and a meeting that
  cannot be dispatched yet (no platform principal) is retried with backoff. Returns `(org_id,
  meeting_id, attempts)`; the worker then reads the meeting's requester under that org's row-level
  security and dispatches it as them (`Meetings.start_scheduled`).

  `SECURITY DEFINER`, owned by the NOLOGIN role `andai_scan`, executable by `andai_app` only (like
  0006, 0009, 0010). `andai_scan` gets SELECT on exactly the meeting columns it reads that earlier
  migrations did not grant (never the link, the title or who asked), and UPDATE on the two claim
  columns; the `scan_read` / `scan_claim` policies of 0004/0006 cover the rows.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "andai_app"
SCAN_ROLE = "andai_scan"
CLAIM = "andai_claim_scheduled(integer, integer, timestamptz)"
LEASE_BASE_S = 30
LEASE_DOUBLINGS = 5
LEASE_MAX = "15 minutes"

# The meeting columns the claim reads beyond those granted before (id, org_id, status: 0004/0006;
# deleted_at: 0009), and the two it writes.
SCAN_SELECT = (
    "scheduled_start",
    "skipped_at",
    "dispatch_attempts",
    "dispatch_lease_until",
)
SCAN_UPDATE = ("dispatch_attempts", "dispatch_lease_until")
DUE = "status = 'scheduled' AND skipped_at IS NULL"


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

    op.add_column(
        "meetings", sa.Column("scheduled_end", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "meetings",
        sa.Column("dispatch_attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "meetings", sa.Column("dispatch_lease_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_meetings_scheduled_due",
        "meetings",
        ["scheduled_start"],
        postgresql_where=sa.text(DUE),
    )

    op.execute(f"GRANT SELECT ({', '.join(SCAN_SELECT)}) ON meetings TO {SCAN_ROLE}")
    op.execute(f"GRANT UPDATE ({', '.join(SCAN_UPDATE)}) ON meetings TO {SCAN_ROLE}")
    op.execute(
        f"""
        CREATE FUNCTION {CLAIM}
        RETURNS TABLE (org_id uuid, meeting_id uuid, attempts integer)
        LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          WITH due AS (
            SELECT m.id
              FROM meetings m
             WHERE m.status = 'scheduled'
               AND m.skipped_at IS NULL
               AND m.deleted_at IS NULL
               AND m.scheduled_start IS NOT NULL
               AND m.scheduled_start - make_interval(secs => greatest($2, 0)) <= $3
               AND (m.dispatch_lease_until IS NULL OR m.dispatch_lease_until <= $3)
             ORDER BY m.scheduled_start, m.id
             LIMIT greatest($1, 0)
             FOR UPDATE SKIP LOCKED
          )
          UPDATE meetings AS t
             SET dispatch_attempts = t.dispatch_attempts + 1,
                 dispatch_lease_until = $3 + least(
                   make_interval(secs => {LEASE_BASE_S}
                     * power(2, least(t.dispatch_attempts, {LEASE_DOUBLINGS}))),
                   interval '{LEASE_MAX}'
                 )
            FROM due
           WHERE t.id = due.id
          RETURNING t.org_id, t.id, t.dispatch_attempts
        $$
        """
    )
    _hand_to_scan(CLAIM)


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {CLAIM}")
    op.execute(f"REVOKE UPDATE ({', '.join(SCAN_UPDATE)}) ON meetings FROM {SCAN_ROLE}")
    op.execute(f"REVOKE SELECT ({', '.join(SCAN_SELECT)}) ON meetings FROM {SCAN_ROLE}")
    op.drop_index("ix_meetings_scheduled_due", table_name="meetings")
    op.drop_column("meetings", "dispatch_lease_until")
    op.drop_column("meetings", "dispatch_attempts")
    op.drop_column("meetings", "scheduled_end")
