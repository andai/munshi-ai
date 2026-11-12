"""Pipeline backlogs, failures and stage durations, and intake counts, for andai-worker's metrics.

Three functions answer the worker's `/metrics` scrape across every org with aggregates only: no id,
org, link, title, requester, sender, subject or raw message ever leaves them.

* `andai_pipeline_stats(at, lead_s)` returns one row per stage, in this order: `dispatch`,
  `ingest`, `minutes`, `recap`, `deletion`, `intake`. Each row has `pending` (work still owed at
  `at`), `oldest_s` (seconds since the oldest pending item became due, 0 when none) and
  `failed_24h` (items that failed for good in the 24 hours before `at`). Pending items and the time
  each became due:
  - `dispatch`: scheduled meetings, not skipped, not deleted, still `scheduled` and due
    (`scheduled_start - lead_s <= at`; `lead_s` is `ANDAI_INTAKE_JOIN_LEAD_S`). Due at
    `scheduled_start - lead_s`.
  - `ingest`: finished meetings with a platform id whose transcript copy is still owed
    (`transcript_state` `none` or `pending`, not deleted; the set `andai_claim_ingest` takes from).
    Due at the meeting's end.
  - `minutes`: finished meetings with a stored transcript whose minutes are owed (`mom_state`
    `none` or `pending`, not deleted, not a canary; the set `andai_mom_stats` counts). Due when the
    transcript was stored.
  - `recap`: `recap_state = 'pending'`, not deleted, not a canary. Due when the first minutes were
    stored.
  - `deletion`: `deletion_state = 'pending'`. Due when the meeting was deleted.
  - `intake`: `inbound_mail` rows `received` or `processing`, org-less rows included. Due when
    received.

  A failure counts in `failed_24h` by the time it can be dated: `dispatch` — a scheduled meeting
  that ended `failed` with no platform meeting (no bot was ever sent), by `ended_at`; `ingest`,
  `minutes` and `recap` — `transcript_state`, `mom_state` or `recap_state` `failed`, by the
  meeting's `ended_at` (those columns carry no failure time); `deletion` — `deletion_state =
  'failed'`, by `deleted_at`; `intake` — a message quarantined `parse_error` after
  `INTAKE_MAX_ATTEMPTS` claims (processing gave up), by `received_at`.

* `andai_pipeline_durations(since, lead_s)` returns `(stage, seconds)` for the histograms, at most
  5000 rows per stage, newest first, of items finished since `since`:
  - `dispatch`: due (`scheduled_start - lead_s`) → `started_at` (the bot recording), for
    scheduled meetings that started since `since`;
  - `ingest`: `ended_at` → `transcript_at` for transcripts stored since `since`;
  - `minutes`: `transcript_at` → `mom_at` (the first minutes) for minutes stored since `since`;
  - `recap`: `mom_at` → `recap_at` for recaps sent since `since`.
  A negative span (a clock step, a lead changed later) counts as 0.

* `andai_intake_stats(since)` returns `(status, outcome, reason, n)`: the `inbound_mail` rows
  received since `since` grouped by status, outcome and `quarantine_reason` ('' for none), org-less
  rows included. All three columns are closed sets (CHECK constraints of 0012); `ignore_reason` is
  free text and is never read.

All three are `SECURITY DEFINER`, owned by the NOLOGIN role `andai_scan`, executable by `andai_app`
only (like 0005, 0009 and 0010). `andai_scan` gets SELECT on the columns they read that earlier
migrations did not grant: `meetings.transcript_at`, `meetings.recap_at`, `inbound_mail.outcome` and
`inbound_mail.quarantine_reason`. The `scan_read` policies of 0004 (meetings) and 0012
(inbound_mail, every row) cover the rows. The downgrade drops the functions and revokes exactly
those four grants.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "andai_app"
SCAN_ROLE = "andai_scan"
STATS = "andai_pipeline_stats(timestamptz, integer)"
DURATIONS = "andai_pipeline_durations(timestamptz, integer)"
INTAKE = "andai_intake_stats(timestamptz)"

STAGES = ("dispatch", "ingest", "minutes", "recap", "deletion", "intake")
# andai-intake's processor gives a message up after this many claims (`processor.MAX_ATTEMPTS`).
INTAKE_MAX_ATTEMPTS = 5
DURATIONS_PER_STAGE = 5000
TERMINAL_IN = "('done', 'failed', 'cancelled')"
WINDOW = "interval '24 hours'"
LEAD = "make_interval(secs => $2)"

# Columns the functions read beyond those granted before (0004, 0005, 0006, 0009, 0010, 0012, 0016).
SCAN_SELECT_MEETINGS = ("transcript_at", "recap_at")
SCAN_SELECT_INBOUND = ("outcome", "quarantine_reason")

NOT_HIDDEN = "deleted_at IS NULL AND deletion_state = 'none' AND source <> 'canary'"
DISPATCH_PENDING = (
    "status = 'scheduled' AND skipped_at IS NULL AND deleted_at IS NULL"
    f" AND scheduled_start IS NOT NULL AND scheduled_start - {LEAD} <= $1"
)
DISPATCH_FAILED = (
    "scheduled_start IS NOT NULL AND status = 'failed' AND platform_meeting_id IS NULL"
    f" AND ended_at > $1 - {WINDOW}"
)
INGEST_PENDING = (
    f"status IN {TERMINAL_IN} AND platform_meeting_id IS NOT NULL"
    " AND transcript_state IN ('none', 'pending') AND deleted_at IS NULL"
)
INGEST_FAILED = f"transcript_state = 'failed' AND ended_at > $1 - {WINDOW}"
MINUTES_PENDING = (
    f"status IN {TERMINAL_IN} AND transcript_state = 'stored'"
    f" AND mom_state IN ('none', 'pending') AND {NOT_HIDDEN}"
)
MINUTES_FAILED = f"mom_state = 'failed' AND source <> 'canary' AND ended_at > $1 - {WINDOW}"
RECAP_PENDING = f"recap_state = 'pending' AND {NOT_HIDDEN}"
RECAP_FAILED = f"recap_state = 'failed' AND source <> 'canary' AND ended_at > $1 - {WINDOW}"
DELETION_PENDING = "deletion_state = 'pending'"
DELETION_FAILED = f"deletion_state = 'failed' AND deleted_at > $1 - {WINDOW}"
INTAKE_PENDING = "status IN ('received', 'processing')"
INTAKE_FAILED = (
    "status = 'quarantined' AND quarantine_reason = 'parse_error'"
    f" AND attempts >= {INTAKE_MAX_ATTEMPTS} AND received_at > $1 - {WINDOW}"
)


def _age(due: str, where: str) -> str:
    """Seconds since the oldest item matching `where` became due at `due`."""
    return f"max(extract(epoch FROM $1 - ({due}))::double precision) FILTER (WHERE {where})"


def _durations(stage: str, seconds: str, finished: str, where: str) -> str:
    return (
        f"(SELECT '{stage}'::text, greatest({seconds}, 0)::double precision"
        f"   FROM meetings WHERE {where} AND {finished} >= $1"
        f"  ORDER BY {finished} DESC LIMIT {DURATIONS_PER_STAGE})"
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

    op.execute(f"GRANT SELECT ({', '.join(SCAN_SELECT_MEETINGS)}) ON meetings TO {SCAN_ROLE}")
    op.execute(f"GRANT SELECT ({', '.join(SCAN_SELECT_INBOUND)}) ON inbound_mail TO {SCAN_ROLE}")

    op.execute(
        f"""
        CREATE FUNCTION {STATS}
        RETURNS TABLE (stage text, pending bigint, oldest_s double precision, failed_24h bigint)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          WITH m AS (
            SELECT
              count(*) FILTER (WHERE {DISPATCH_PENDING}) AS dispatch_n,
              {_age(f"scheduled_start - {LEAD}", DISPATCH_PENDING)} AS dispatch_age,
              count(*) FILTER (WHERE {DISPATCH_FAILED}) AS dispatch_failed,
              count(*) FILTER (WHERE {INGEST_PENDING}) AS ingest_n,
              {_age("coalesce(ended_at, created_at)", INGEST_PENDING)} AS ingest_age,
              count(*) FILTER (WHERE {INGEST_FAILED}) AS ingest_failed,
              count(*) FILTER (WHERE {MINUTES_PENDING}) AS minutes_n,
              {_age("coalesce(transcript_at, ended_at, created_at)", MINUTES_PENDING)}
                AS minutes_age,
              count(*) FILTER (WHERE {MINUTES_FAILED}) AS minutes_failed,
              count(*) FILTER (WHERE {RECAP_PENDING}) AS recap_n,
              {_age("coalesce(mom_at, ended_at, created_at)", RECAP_PENDING)} AS recap_age,
              count(*) FILTER (WHERE {RECAP_FAILED}) AS recap_failed,
              count(*) FILTER (WHERE {DELETION_PENDING}) AS deletion_n,
              {_age("deleted_at", DELETION_PENDING)} AS deletion_age,
              count(*) FILTER (WHERE {DELETION_FAILED}) AS deletion_failed
              FROM meetings
          ), i AS (
            SELECT
              count(*) FILTER (WHERE {INTAKE_PENDING}) AS intake_n,
              {_age("received_at", INTAKE_PENDING)} AS intake_age,
              count(*) FILTER (WHERE {INTAKE_FAILED}) AS intake_failed
              FROM inbound_mail
          )
          SELECT s.stage, s.pending, greatest(coalesce(s.oldest, 0), 0), s.failed
            FROM m, i, LATERAL (VALUES
              (1, 'dispatch'::text, m.dispatch_n, m.dispatch_age, m.dispatch_failed),
              (2, 'ingest', m.ingest_n, m.ingest_age, m.ingest_failed),
              (3, 'minutes', m.minutes_n, m.minutes_age, m.minutes_failed),
              (4, 'recap', m.recap_n, m.recap_age, m.recap_failed),
              (5, 'deletion', m.deletion_n, m.deletion_age, m.deletion_failed),
              (6, 'intake', i.intake_n, i.intake_age, i.intake_failed)
            ) AS s(n, stage, pending, oldest, failed)
           ORDER BY s.n
        $$
        """
    )
    _hand_to_scan(STATS)

    durations = "\n          UNION ALL\n          ".join(
        (
            _durations(
                "dispatch",
                f"extract(epoch FROM started_at - (scheduled_start - {LEAD}))",
                "started_at",
                "scheduled_start IS NOT NULL AND started_at IS NOT NULL",
            ),
            _durations(
                "ingest",
                "extract(epoch FROM transcript_at - ended_at)",
                "transcript_at",
                "transcript_state = 'stored' AND transcript_at IS NOT NULL"
                " AND ended_at IS NOT NULL",
            ),
            _durations(
                "minutes",
                "extract(epoch FROM mom_at - transcript_at)",
                "mom_at",
                "mom_at IS NOT NULL AND transcript_at IS NOT NULL AND source <> 'canary'",
            ),
            _durations(
                "recap",
                "extract(epoch FROM recap_at - mom_at)",
                "recap_at",
                "recap_state = 'sent' AND recap_at IS NOT NULL AND mom_at IS NOT NULL"
                " AND source <> 'canary'",
            ),
        )
    )
    op.execute(
        f"""
        CREATE FUNCTION {DURATIONS}
        RETURNS TABLE (stage text, seconds double precision)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          {durations}
        $$
        """
    )
    _hand_to_scan(DURATIONS)

    op.execute(
        f"""
        CREATE FUNCTION {INTAKE}
        RETURNS TABLE (status text, outcome text, reason text, n bigint)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          SELECT status, coalesce(outcome, ''), coalesce(quarantine_reason, ''), count(*)::bigint
            FROM inbound_mail
           WHERE received_at >= $1
           GROUP BY 1, 2, 3
           ORDER BY 1, 2, 3
        $$
        """
    )
    _hand_to_scan(INTAKE)


def downgrade() -> None:
    for function in (INTAKE, DURATIONS, STATS):
        op.execute(f"DROP FUNCTION {function}")
    op.execute(
        f"REVOKE SELECT ({', '.join(SCAN_SELECT_INBOUND)}) ON inbound_mail FROM {SCAN_ROLE}"
    )
    op.execute(f"REVOKE SELECT ({', '.join(SCAN_SELECT_MEETINGS)}) ON meetings FROM {SCAN_ROLE}")
