"""Canary org flag, canary runs, alert state, and the cross-org join statistics.

* `orgs.is_canary` marks the one org whose principal sends canary bots (`python -m andai_core
  canary-setup`); a partial unique index allows at most one. Customer orgs never see it: it is an
  org like any other under row-level security.
* `andai_canary_org_id()` returns that org's id (or NULL) to `andai_app` with no org context, so
  andai-worker can find it. Like 0003/0004 it is `SECURITY DEFINER`, owned by the NOLOGIN role
  `andai_scan`, which may read only the `id` and `is_canary` columns of `orgs` through the
  role-scoped policy `scan_read`.
* `canary_runs` and `alert_state` are system tables with no org and no customer data: a canary
  run holds its platform, timestamps, outcome, failure reason and the canary meeting's id; an alert
  row holds one incident's kind, platform, open/closed times and the mail still owed. No row-level
  security; `andai_app` may read, insert and update them. One scheduled-or-running run per platform
  (partial unique index), so two workers never schedule the same platform twice.
* `andai_join_stats(since)` aggregates bot joins across orgs since `since` into
  `(platform, source, outcome, reason, count)` rows, classified per D-39: a join succeeds when the
  bot reached `recording`; meetings cancelled by their user, denied by the host
  (`admission_denied`) or removed by participants are `excluded`; a failed join is `failure` with
  its `JoinFailureReason`; a meeting still on its way in is `pending`. No id, link, name or org
  leaves the function. `andai_scan` gets column grants on exactly the meeting columns it reads.

Each function is handed to `andai_scan` with CREATE on schema public granted for the transfer and
revoked straight after.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "andai_app"
SCAN_ROLE = "andai_scan"
CANARY_FUNCTION = "andai_canary_org_id()"
STATS_FUNCTION = "andai_join_stats(timestamptz)"

PLATFORMS = ("google_meet", "teams", "zoom")
RUN_STATES = ("scheduled", "running", "done")
OUTCOMES = ("success", "failure")
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
ALERT_KINDS = ("canary_failures", "join_rate")
NOTICES = ("open", "recovery")
# The meeting columns andai_join_stats reads (org_id and status were granted by 0004).
STATS_COLUMNS = ("platform", "source", "failure_reason", "end_reason", "started_at", "created_at")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ts(name: str, *, nullable: bool = True, now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if now else None,
    )


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

    # ── the canary org ──
    op.add_column(
        "orgs",
        sa.Column("is_canary", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    op.create_index(
        "uq_orgs_is_canary", "orgs", ["is_canary"], unique=True, postgresql_where=sa.text("is_canary")
    )
    op.execute(f"GRANT SELECT (id, is_canary) ON orgs TO {SCAN_ROLE}")
    op.execute(
        f"CREATE POLICY scan_read ON orgs AS PERMISSIVE FOR SELECT TO {SCAN_ROLE} USING (true)"
    )
    op.execute(
        f"""
        CREATE FUNCTION {CANARY_FUNCTION} RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$ SELECT id FROM orgs WHERE is_canary $$
        """
    )
    _hand_to_scan(CANARY_FUNCTION)

    # ── system tables ──
    op.create_table(
        "canary_runs",
        sa.Column(
            "id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("platform", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        _ts("due_at", nullable=False),
        _ts("started_at"),
        _ts("recorded_at"),
        _ts("finished_at"),
        sa.Column("outcome", sa.Text, nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("meeting_id", pg.UUID(as_uuid=True), nullable=True),
        _ts("created_at", nullable=False, now=True),
        sa.CheckConstraint(_in("platform", PLATFORMS), name="ck_canary_runs_platform"),
        sa.CheckConstraint(_in("state", RUN_STATES), name="ck_canary_runs_state"),
        sa.CheckConstraint(
            f"outcome IS NULL OR {_in('outcome', OUTCOMES)}", name="ck_canary_runs_outcome"
        ),
        sa.CheckConstraint(
            f"reason IS NULL OR {_in('reason', FAILURE_REASONS)}", name="ck_canary_runs_reason"
        ),
        sa.CheckConstraint(
            "(outcome = 'failure') = (reason IS NOT NULL)", name="ck_canary_runs_failure_has_reason"
        ),
        sa.CheckConstraint(
            "state <> 'done' OR (outcome IS NOT NULL AND finished_at IS NOT NULL)",
            name="ck_canary_runs_done_has_outcome",
        ),
    )
    op.create_index(
        "uq_canary_runs_pending",
        "canary_runs",
        ["platform"],
        unique=True,
        postgresql_where=sa.text("state IN ('scheduled', 'running')"),
    )
    op.create_index("ix_canary_runs_platform_finished_at", "canary_runs", ["platform", "finished_at"])

    op.create_table(
        "alert_state",
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("platform", sa.Text, nullable=False),
        sa.Column("open", sa.Boolean, nullable=False, server_default=sa.text("false")),
        _ts("opened_at"),
        _ts("closed_at"),
        sa.Column("notice", sa.Text, nullable=True),
        _ts("updated_at", nullable=False, now=True),
        sa.PrimaryKeyConstraint("kind", "platform", name="pk_alert_state"),
        sa.CheckConstraint(_in("kind", ALERT_KINDS), name="ck_alert_state_kind"),
        sa.CheckConstraint(_in("platform", PLATFORMS), name="ck_alert_state_platform"),
        sa.CheckConstraint(
            f"notice IS NULL OR {_in('notice', NOTICES)}", name="ck_alert_state_notice"
        ),
    )
    for table in ("canary_runs", "alert_state"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {APP_ROLE}")

    # ── cross-org join statistics ──
    op.execute(f"GRANT SELECT ({', '.join(STATS_COLUMNS)}) ON meetings TO {SCAN_ROLE}")
    op.execute(
        f"""
        CREATE FUNCTION {STATS_FUNCTION}
        RETURNS TABLE (platform text, source text, outcome text, reason text, count bigint)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          SELECT j.platform, j.source, j.outcome, j.reason, count(*)::bigint
            FROM (
              SELECT m.platform, m.source,
                     CASE
                       WHEN m.status = 'cancelled' THEN 'excluded'
                       WHEN m.failure_reason IN ('admission_denied', 'removed_by_participant')
                         THEN 'excluded'
                       WHEN m.end_reason = 'evicted' THEN 'excluded'
                       WHEN m.started_at IS NOT NULL
                         OR m.status IN ('recording', 'processing', 'done') THEN 'success'
                       WHEN m.status = 'failed' THEN 'failure'
                       ELSE 'pending'
                     END AS outcome,
                     CASE
                       WHEN m.status = 'cancelled' THEN 'cancelled'
                       WHEN m.failure_reason IN ('admission_denied', 'removed_by_participant')
                         THEN m.failure_reason
                       WHEN m.end_reason = 'evicted' THEN 'removed_by_participant'
                       WHEN m.started_at IS NOT NULL
                         OR m.status IN ('recording', 'processing', 'done') THEN NULL
                       WHEN m.status = 'failed' THEN m.failure_reason
                       ELSE NULL
                     END AS reason
                FROM meetings m
               WHERE m.created_at >= $1
            ) j
           GROUP BY j.platform, j.source, j.outcome, j.reason
        $$
        """
    )
    _hand_to_scan(STATS_FUNCTION)


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {STATS_FUNCTION}")
    op.execute(f"REVOKE SELECT ({', '.join(STATS_COLUMNS)}) ON meetings FROM {SCAN_ROLE}")
    op.drop_table("alert_state")
    op.drop_table("canary_runs")
    op.execute(f"DROP FUNCTION {CANARY_FUNCTION}")
    op.execute("DROP POLICY scan_read ON orgs")
    op.execute(f"REVOKE SELECT (id, is_canary) ON orgs FROM {SCAN_ROLE}")
    op.drop_index("uq_orgs_is_canary", table_name="orgs")
    op.drop_column("orgs", "is_canary")
