"""The minutes pipeline: per-meeting MoM and recap state, the platform webhook's dedup table, and
the cross-org claims andai-worker runs them through.

* `meetings` gains these columns:
  - the MoM state: `mom_state` (`none | pending | stored | failed | skipped`), `mom_attempts`,
    `mom_next_at` (backoff), `mom_at` (when the meeting's first minutes were stored), `mom_error`
    (the typed code of the last failure), `mom_request` (template and language of a pending
    regenerate) and the lease `mom_lease` / `mom_lease_until`;
  - the recap state: `recap_state` (`none | pending | sent | failed | skipped`), `recap_attempts`,
    `recap_next_at` (due time, backoff and lease), `recap_lease` and `recap_at` (when it was sent).
* Meetings that are already over when 0010 runs are never generated retroactively: those with
  minutes become `mom_state = stored`, the rest `skipped`, and all of them `recap_state = skipped`.
  A meeting still running stays `none` and gets minutes when it ends.
* `platform_webhook_events` keeps one row per platform webhook event andai-core accepted
  (`event_id`, the dedup key, `event_type`, `platform_meeting_id`, `received_at`). It holds no
  org and no customer data, so it has no row-level security; `andai_app` may SELECT and INSERT.
* Three functions, `SECURITY DEFINER`, owned by the NOLOGIN role `andai_scan`, executable by
  `andai_app` only (like 0006 and 0009):
  - `andai_claim_mom(max)` hands andai-worker at most `max` meetings whose minutes are due: over,
    transcript stored, `mom_state` `none` or `pending`, not a canary, not deleted, fewer than 8
    attempts, `mom_next_at` passed and no live lease. Each is taken `FOR UPDATE SKIP LOCKED`, marked
    `pending` and given a fresh lease token for 15 minutes (longer than a full map-reduce run at the
    client timeout). Returns `(org_id, meeting_id, lease)`.
  - `andai_claim_recap(max)` does the same for recaps that are `pending`, due and under 5 attempts
    (canaries and deleted meetings excluded), leasing each for 2 minutes through `recap_next_at`.
  - `andai_platform_completed(platform_meeting_id)` is the webhook's nudge: when that meeting's
    transcript copy is waiting for a retry, it is made due now. It returns nothing, so the caller
    learns nothing about which meetings exist.
  - `andai_mom_stats(since)` answers the worker's metrics: the number of meetings whose minutes are
    still owed, the number that failed, and the seconds from meeting end to first minutes for
    meetings whose first minutes were stored since `since`. No id, name or org leaves it.

  `andai_scan` gets SELECT on exactly the meeting columns the functions read and UPDATE on the
  lease and state columns they write; the `scan_claim` / `scan_read` policies of 0004/0006 cover
  the rows.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "andai_app"
SCAN_ROLE = "andai_scan"
CLAIM_MOM = "andai_claim_mom(integer)"
CLAIM_RECAP = "andai_claim_recap(integer)"
COMPLETED = "andai_platform_completed(bigint)"
STATS = "andai_mom_stats(timestamptz)"

MOM_STATES = ("none", "pending", "stored", "failed", "skipped")
RECAP_STATES = ("none", "pending", "sent", "failed", "skipped")
TERMINAL_IN = "('done', 'failed', 'cancelled')"
MOM_MAX_ATTEMPTS = 8
MOM_LEASE = "15 minutes"
RECAP_MAX_ATTEMPTS = 5
RECAP_LEASE = "2 minutes"

SCAN_SELECT = (
    "ended_at",
    "mom_state",
    "mom_attempts",
    "mom_next_at",
    "mom_at",
    "mom_lease",
    "mom_lease_until",
    "recap_state",
    "recap_attempts",
    "recap_next_at",
    "recap_lease",
)
SCAN_UPDATE = ("mom_state", "mom_lease", "mom_lease_until", "recap_lease", "recap_next_at")
MOM_DUE = (
    f"status IN {TERMINAL_IN} AND transcript_state = 'stored'"
    " AND mom_state IN ('none', 'pending')"
)
NOT_HIDDEN = "m.deleted_at IS NULL AND m.deletion_state = 'none' AND m.source <> 'canary'"
COLUMNS = (
    "recap_at",
    "recap_lease",
    "recap_next_at",
    "recap_attempts",
    "recap_state",
    "mom_lease_until",
    "mom_lease",
    "mom_request",
    "mom_error",
    "mom_at",
    "mom_next_at",
    "mom_attempts",
    "mom_state",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _hand_to_scan(function: str) -> None:
    op.execute(f"GRANT CREATE ON SCHEMA public TO {SCAN_ROLE}")
    op.execute(f"ALTER FUNCTION {function} OWNER TO {SCAN_ROLE}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {SCAN_ROLE}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {function} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO {APP_ROLE}")


def _ts(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def _count(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer, nullable=False, server_default=sa.text("0"))


def _state(name: str) -> sa.Column:
    return sa.Column(name, sa.Text, nullable=False, server_default=sa.text("'none'"))


def upgrade() -> None:
    exists = op.get_bind().execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": SCAN_ROLE}
    )
    if exists.first() is None:
        raise RuntimeError(
            f"role {SCAN_ROLE} does not exist; run db-init.sql first (make -C andai db-init)"
        )

    # ── meetings: the MoM and recap state ──
    for column in (
        _state("mom_state"),
        _count("mom_attempts"),
        _ts("mom_next_at"),
        _ts("mom_at"),
        sa.Column("mom_error", sa.Text, nullable=True),
        sa.Column("mom_request", pg.JSONB, nullable=True),
        sa.Column("mom_lease", pg.UUID(as_uuid=True), nullable=True),
        _ts("mom_lease_until"),
        _state("recap_state"),
        _count("recap_attempts"),
        _ts("recap_next_at"),
        sa.Column("recap_lease", pg.UUID(as_uuid=True), nullable=True),
        _ts("recap_at"),
    ):
        op.add_column("meetings", column)
    op.execute(
        "ALTER TABLE meetings ADD CONSTRAINT ck_meetings_mom_state"
        f" CHECK ({_in('mom_state', MOM_STATES)})"
    )
    op.execute(
        "ALTER TABLE meetings ADD CONSTRAINT ck_meetings_recap_state"
        f" CHECK ({_in('recap_state', RECAP_STATES)})"
    )
    op.create_index(
        "ix_meetings_mom_due", "meetings", ["mom_next_at"], postgresql_where=sa.text(MOM_DUE)
    )
    op.create_index(
        "ix_meetings_recap_due",
        "meetings",
        ["recap_next_at"],
        postgresql_where=sa.text("recap_state = 'pending'"),
    )
    op.create_index("ix_meetings_platform_meeting_id", "meetings", ["platform_meeting_id"])

    # ── no retroactive minutes or recaps for meetings already over ──
    # FORCE ROW LEVEL SECURITY binds the owner too; lifted for this one statement, inside the
    # migration's transaction.
    for table in ("meetings", "mom_documents"):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        UPDATE meetings AS m
           SET mom_state = CASE WHEN f.first_at IS NULL THEN 'skipped' ELSE 'stored' END,
               mom_at = f.first_at,
               recap_state = 'skipped'
          FROM (
            SELECT mm.id, (SELECT min(d.created_at) FROM mom_documents d
                            WHERE d.meeting_id = mm.id) AS first_at
              FROM meetings mm
             WHERE mm.status IN {TERMINAL_IN}
          ) AS f
         WHERE m.id = f.id
        """
    )
    for table in ("meetings", "mom_documents"):
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # ── the platform webhook's dedup table (no org data) ──
    op.create_table(
        "platform_webhook_events",
        sa.Column("event_id", sa.Text, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("platform_meeting_id", sa.BigInteger, nullable=True),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_platform_webhook_events"),
    )
    op.execute(f"GRANT SELECT, INSERT ON platform_webhook_events TO {APP_ROLE}")

    # ── the cross-org functions ──
    op.execute(f"GRANT SELECT ({', '.join(SCAN_SELECT)}) ON meetings TO {SCAN_ROLE}")
    op.execute(f"GRANT UPDATE ({', '.join(SCAN_UPDATE)}) ON meetings TO {SCAN_ROLE}")
    op.execute(
        f"""
        CREATE FUNCTION {CLAIM_MOM}
        RETURNS TABLE (org_id uuid, meeting_id uuid, lease uuid)
        LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          WITH due AS (
            SELECT m.id
              FROM meetings m
             WHERE m.status IN {TERMINAL_IN}
               AND m.transcript_state = 'stored'
               AND m.mom_state IN ('none', 'pending')
               AND {NOT_HIDDEN}
               AND m.mom_attempts < {MOM_MAX_ATTEMPTS}
               AND (m.mom_next_at IS NULL OR m.mom_next_at <= now())
               AND (m.mom_lease_until IS NULL OR m.mom_lease_until <= now())
             ORDER BY m.mom_next_at NULLS FIRST, m.id
             LIMIT greatest($1, 0)
             FOR UPDATE SKIP LOCKED
          )
          UPDATE meetings AS t
             SET mom_state = 'pending',
                 mom_lease = gen_random_uuid(),
                 mom_lease_until = now() + interval '{MOM_LEASE}'
            FROM due
           WHERE t.id = due.id
          RETURNING t.org_id, t.id, t.mom_lease
        $$
        """
    )
    _hand_to_scan(CLAIM_MOM)
    op.execute(
        f"""
        CREATE FUNCTION {CLAIM_RECAP}
        RETURNS TABLE (org_id uuid, meeting_id uuid, lease uuid)
        LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          WITH due AS (
            SELECT m.id
              FROM meetings m
             WHERE m.recap_state = 'pending'
               AND {NOT_HIDDEN}
               AND m.recap_attempts < {RECAP_MAX_ATTEMPTS}
               AND (m.recap_next_at IS NULL OR m.recap_next_at <= now())
             ORDER BY m.recap_next_at NULLS FIRST, m.id
             LIMIT greatest($1, 0)
             FOR UPDATE SKIP LOCKED
          )
          UPDATE meetings AS t
             SET recap_lease = gen_random_uuid(),
                 recap_next_at = now() + interval '{RECAP_LEASE}'
            FROM due
           WHERE t.id = due.id
          RETURNING t.org_id, t.id, t.recap_lease
        $$
        """
    )
    _hand_to_scan(CLAIM_RECAP)
    op.execute(
        f"""
        CREATE FUNCTION {COMPLETED}
        RETURNS void
        LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          UPDATE meetings AS m
             SET transcript_next_at = now()
           WHERE m.platform_meeting_id = $1
             AND m.deletion_state = 'none'
             AND m.transcript_state IN ('none', 'pending')
             AND m.transcript_next_at > now()
        $$
        """
    )
    _hand_to_scan(COMPLETED)
    op.execute(
        f"""
        CREATE FUNCTION {STATS}
        RETURNS TABLE (pending bigint, failed bigint, latencies double precision[])
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          SELECT
            count(*) FILTER (
              WHERE m.status IN {TERMINAL_IN} AND m.transcript_state = 'stored'
                AND m.mom_state IN ('none', 'pending')
            )::bigint,
            count(*) FILTER (WHERE m.mom_state = 'failed')::bigint,
            coalesce(
              array_agg(extract(epoch FROM m.mom_at - m.ended_at)::double precision
                        ORDER BY m.mom_at)
                FILTER (WHERE m.mom_at >= $1 AND m.ended_at IS NOT NULL
                          AND m.mom_at >= m.ended_at),
              '{{}}'::double precision[]
            )
            FROM meetings m
           WHERE {NOT_HIDDEN}
        $$
        """
    )
    _hand_to_scan(STATS)


def downgrade() -> None:
    for function in (STATS, COMPLETED, CLAIM_RECAP, CLAIM_MOM):
        op.execute(f"DROP FUNCTION {function}")
    op.execute(f"REVOKE UPDATE ({', '.join(SCAN_UPDATE)}) ON meetings FROM {SCAN_ROLE}")
    op.execute(f"REVOKE SELECT ({', '.join(SCAN_SELECT)}) ON meetings FROM {SCAN_ROLE}")
    op.drop_table("platform_webhook_events")
    op.drop_index("ix_meetings_platform_meeting_id", table_name="meetings")
    op.drop_index("ix_meetings_recap_due", table_name="meetings")
    op.drop_index("ix_meetings_mom_due", table_name="meetings")
    op.execute("ALTER TABLE meetings DROP CONSTRAINT ck_meetings_recap_state")
    op.execute("ALTER TABLE meetings DROP CONSTRAINT ck_meetings_mom_state")
    for column in COLUMNS:
        op.drop_column("meetings", column)
