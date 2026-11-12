"""User erasure (OPS-02): the request table, the tombstone user and the functions andai-worker and
the erasure code reach other orgs' rows through.

* `users.status` may be `erased`: an account whose person was erased. Its row stays (other orgs'
  rows may still point at it) but carries no identity: `erased-<request id>@munshi.invalid`,
  `Erased user`, no password.
* The tombstone user `TOMBSTONE_USER_ID` (`erased-user@munshi.invalid`, `Erased user`, status
  `erased`, no password, no membership): references to an erased person in an org's remaining rows
  (who granted a share, who deleted or skipped a meeting, who sent an invite, who changed a
  setting) are re-pointed at it.
* `erasure_requests`: one row per (org, person) erasure. `org_id` is the org whose data is scrubbed
  (NULL only for an account-only request of the operator), `audit_org_id` the org every audit
  event of the request is written in (equal to `org_id` when that is set, so `audit_events.org_id`
  stays NOT NULL). `subject_user_id` and `requested_by` are plain ids (no foreign key: the trail
  outlives the user row). `source` (`org_admin`, `operator`), `account` (anonymise the account
  once no membership remains), `state` (`pending`, `running`, `done`, `failed`), `step` (where the
  runner is: `stop_live`, `delete_owned`, `wait_purge`, `scrub`, `account`, `done`), `attempts`,
  `next_at`, `lease_until`, `error` (a closed set: `purge_failed`, `internal_error`), `counts`
  (rows touched per table; no names), `created_at`, `finished_at`. At most one open request per
  (audit org, person). FORCE ROW LEVEL SECURITY on `audit_org_id`; `andai_app` may SELECT, INSERT
  and UPDATE (a request is never deleted).
* Functions, `SECURITY DEFINER`, owned by the NOLOGIN role `andai_scan`, executable by `andai_app`
  only (like 0009 and 0018):
  - `andai_claim_erasures(max, at)` hands andai-worker at most `max` open requests of any org that
    are due (`next_at` passed, lease expired) as `(audit_org_id, request_id, attempts)`, taken
    `FOR UPDATE SKIP LOCKED`, set `running` and leased for 60 s doubling with `attempts` up to 15
    minutes. Account-only requests are included; the worker runs each under its audit org.
  - `andai_erasure_backlog(at)` answers `(pending, oldest_s, failed)`: open requests, seconds since
    the oldest open one was created (0 when none), failed requests. Numbers only.
  - `andai_erasure_siblings(subject, request)` counts the person's other requests that are not
    `done`, in any org: the account step waits until it is 0.
  - `andai_erasure_org(request)` answers a request's audit org (the operator's `erase-status`).
  - `andai_erase_inbound(org, user, email)` deletes the notetaker mailbox's rows of a person: with
    `org` (which must be the transaction's `app.org_id`), that org's rows resolved to `user` or
    sent by `email` (header sender or envelope sender); with `org` NULL, the org-less rows sent by
    `email`. Returns the count. It sets `andai.erasing` to `on` for its delete and back to `off`
    before it returns: the only state in which the role-scoped policy `scan_erase` lets
    `andai_scan` delete a row received in the last day. The 30-day purge's own policy
    (`scan_purge`) is unchanged.
  `andai_scan` gets SELECT and UPDATE column grants on `erasure_requests` for the claim and the
  backlog, role-scoped policies `scan_read` and `scan_claim` there, and SELECT on
  `inbound_mail.org_id`, `resolved_user_id` and `sender`.

The downgrade drops all of it. Erased accounts become `disabled`; the tombstone user is deleted
unless rows still point at it, in which case it stays as a disabled user.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "andai_app"
SCAN_ROLE = "andai_scan"
APP_ORG = "NULLIF(current_setting('app.org_id', true), '')::uuid"

TOMBSTONE_USER_ID = "00000000-0000-4000-8000-00000000e7a5"
TOMBSTONE_EMAIL = "erased-user@munshi.invalid"
TOMBSTONE_NAME = "Erased user"

USER_STATUSES_BEFORE = ("invited", "active", "disabled")
USER_STATUSES = (*USER_STATUSES_BEFORE, "erased")

SOURCES = ("org_admin", "operator")
STATES = ("pending", "running", "done", "failed")
OPEN_STATES = ("pending", "running")
STEPS = ("stop_live", "delete_owned", "wait_purge", "scrub", "account", "done")
ERRORS = ("purge_failed", "internal_error")

CLAIM = "andai_claim_erasures(integer, timestamptz)"
BACKLOG = "andai_erasure_backlog(timestamptz)"
SIBLINGS = "andai_erasure_siblings(uuid, uuid)"
ORG_OF = "andai_erasure_org(uuid)"
ERASE_INBOUND = "andai_erase_inbound(uuid, uuid, text)"
FUNCTIONS = (CLAIM, BACKLOG, SIBLINGS, ORG_OF, ERASE_INBOUND)

LEASE_BASE_S = 60
LEASE_DOUBLINGS = 4
LEASE_MAX = "15 minutes"
ERASING = "andai.erasing"

SCAN_SELECT = (
    "id",
    "audit_org_id",
    "subject_user_id",
    "state",
    "attempts",
    "next_at",
    "lease_until",
    "created_at",
)
SCAN_UPDATE = ("state", "lease_until")
SCAN_SELECT_INBOUND = ("org_id", "resolved_user_id", "sender")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _hand_to_scan(function: str) -> None:
    op.execute(f"GRANT CREATE ON SCHEMA public TO {SCAN_ROLE}")
    op.execute(f"ALTER FUNCTION {function} OWNER TO {SCAN_ROLE}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {SCAN_ROLE}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {function} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO {APP_ROLE}")


def _user_status_check(values: tuple[str, ...]) -> None:
    """Replace the users status CHECK, keeping the name 0001's naming convention gave it."""
    check = _in("status", values).replace("'", "''")
    op.execute(
        f"""
        DO $$
        DECLARE
          name text;
        BEGIN
          SELECT c.conname INTO STRICT name
            FROM pg_constraint c
           WHERE c.conrelid = 'users'::regclass AND c.contype = 'c'
             AND position('status' IN pg_get_constraintdef(c.oid)) > 0;
          EXECUTE 'ALTER TABLE users DROP CONSTRAINT ' || quote_ident(name);
          EXECUTE 'ALTER TABLE users ADD CONSTRAINT ' || quote_ident(name) || ' CHECK ({check})';
        END
        $$
        """
    )


def upgrade() -> None:
    exists = op.get_bind().execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": SCAN_ROLE}
    )
    if exists.first() is None:
        raise RuntimeError(
            f"role {SCAN_ROLE} does not exist; run db-init.sql first (make -C andai db-init)"
        )

    # ── users: the erased status and the tombstone ──
    _user_status_check(USER_STATUSES)
    op.execute(
        "INSERT INTO users (id, email, display_name, status, password_hash)"
        f" VALUES ('{TOMBSTONE_USER_ID}', '{TOMBSTONE_EMAIL}', '{TOMBSTONE_NAME}', 'erased', NULL)"
        # A downgrade keeps the row when rows still point at it; upgrading again restores it.
        " ON CONFLICT (id) DO UPDATE SET email = EXCLUDED.email,"
        " display_name = EXCLUDED.display_name, status = 'erased', password_hash = NULL"
    )

    # ── erasure requests ──
    op.create_table(
        "erasure_requests",
        sa.Column(
            "id", pg.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("org_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("audit_org_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_user_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("requested_by", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("account", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("state", sa.Text, nullable=False, server_default=sa.text("'pending'")),
        sa.Column("step", sa.Text, nullable=False, server_default=sa.text("'stop_live'")),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("next_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column(
            "counts", pg.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_erasure_requests"),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], ondelete="RESTRICT", name="fk_erasure_requests_org_id_orgs"
        ),
        sa.ForeignKeyConstraint(
            ["audit_org_id"],
            ["orgs.id"],
            ondelete="RESTRICT",
            name="fk_erasure_requests_audit_org_id_orgs",
        ),
        sa.CheckConstraint(_in("source", SOURCES), name="source"),
        sa.CheckConstraint(_in("state", STATES), name="state"),
        sa.CheckConstraint(_in("step", STEPS), name="step"),
        sa.CheckConstraint(
            f"error IS NULL OR {_in('error', ERRORS)}", name="error"
        ),
        sa.CheckConstraint(
            "(state = 'failed') = (error IS NOT NULL)", name="failed_has_error"
        ),
        sa.CheckConstraint(
            "org_id IS NULL OR org_id = audit_org_id", name="audit_org"
        ),
    )
    op.create_index(
        "uq_erasure_requests_open",
        "erasure_requests",
        ["audit_org_id", "subject_user_id"],
        unique=True,
        postgresql_where=sa.text(_in("state", OPEN_STATES)),
    )
    op.create_index(
        "ix_erasure_requests_due",
        "erasure_requests",
        ["next_at"],
        postgresql_where=sa.text(_in("state", OPEN_STATES)),
    )
    op.create_index(
        "ix_erasure_requests_audit_org_id_created_at",
        "erasure_requests",
        ["audit_org_id", "created_at", "id"],
    )
    op.create_index(
        "ix_erasure_requests_subject_user_id", "erasure_requests", ["subject_user_id"]
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON erasure_requests TO {APP_ROLE}")
    op.execute("ALTER TABLE erasure_requests ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE erasure_requests FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY org_isolation ON erasure_requests AS PERMISSIVE FOR ALL"
        f" USING (audit_org_id = {APP_ORG}) WITH CHECK (audit_org_id = {APP_ORG})"
    )

    # ── andai_scan: exactly what the functions touch ──
    op.execute(f"GRANT SELECT ({', '.join(SCAN_SELECT)}) ON erasure_requests TO {SCAN_ROLE}")
    op.execute(f"GRANT UPDATE ({', '.join(SCAN_UPDATE)}) ON erasure_requests TO {SCAN_ROLE}")
    op.execute(
        f"CREATE POLICY scan_read ON erasure_requests AS PERMISSIVE FOR SELECT TO {SCAN_ROLE}"
        " USING (true)"
    )
    op.execute(
        f"CREATE POLICY scan_claim ON erasure_requests AS PERMISSIVE FOR UPDATE TO {SCAN_ROLE}"
        f" USING ({_in('state', OPEN_STATES)}) WITH CHECK (state = 'running')"
    )
    op.execute(f"GRANT SELECT ({', '.join(SCAN_SELECT_INBOUND)}) ON inbound_mail TO {SCAN_ROLE}")
    op.execute(
        f"CREATE POLICY scan_erase ON inbound_mail AS PERMISSIVE FOR DELETE TO {SCAN_ROLE}"
        f" USING (current_setting('{ERASING}', true) = 'on')"
    )

    lease = (
        f"least(make_interval(secs => {LEASE_BASE_S}"
        f" * power(2, least(t.attempts, {LEASE_DOUBLINGS}))), interval '{LEASE_MAX}')"
    )
    op.execute(
        f"""
        CREATE FUNCTION {CLAIM}
        RETURNS TABLE (audit_org_id uuid, request_id uuid, attempts integer)
        LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          WITH due AS (
            SELECT r.id
              FROM erasure_requests r
             WHERE r.state IN ('pending', 'running')
               AND (r.next_at IS NULL OR r.next_at <= $2)
               AND (r.lease_until IS NULL OR r.lease_until <= $2)
             ORDER BY r.next_at NULLS FIRST, r.created_at, r.id
             LIMIT greatest($1, 0)
             FOR UPDATE SKIP LOCKED
          )
          UPDATE erasure_requests AS t
             SET state = 'running',
                 lease_until = $2 + {lease}
            FROM due
           WHERE t.id = due.id
          RETURNING t.audit_org_id, t.id, t.attempts
        $$
        """
    )
    _hand_to_scan(CLAIM)
    op.execute(
        f"""
        CREATE FUNCTION {BACKLOG}
        RETURNS TABLE (pending bigint, oldest_s double precision, failed bigint)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          SELECT count(*) FILTER (WHERE r.state IN ('pending', 'running'))::bigint,
                 greatest(coalesce(
                   extract(epoch FROM $1 - min(r.created_at)
                     FILTER (WHERE r.state IN ('pending', 'running')))::double precision,
                   0), 0),
                 count(*) FILTER (WHERE r.state = 'failed')::bigint
            FROM erasure_requests r
        $$
        """
    )
    _hand_to_scan(BACKLOG)
    op.execute(
        f"""
        CREATE FUNCTION {SIBLINGS}
        RETURNS bigint
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          SELECT count(*)::bigint
            FROM erasure_requests r
           WHERE r.subject_user_id = $1
             AND r.id <> $2
             AND r.state <> 'done'
        $$
        """
    )
    _hand_to_scan(SIBLINGS)
    op.execute(
        f"""
        CREATE FUNCTION {ORG_OF}
        RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          SELECT r.audit_org_id FROM erasure_requests r WHERE r.id = $1
        $$
        """
    )
    _hand_to_scan(ORG_OF)
    op.execute(
        f"""
        CREATE FUNCTION {ERASE_INBOUND}
        RETURNS bigint
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        DECLARE
          deleted bigint;
        BEGIN
          IF $3 IS NULL OR btrim($3) = '' THEN
            RAISE EXCEPTION 'andai_erase_inbound: an email address is required';
          END IF;
          PERFORM set_config('{ERASING}', 'on', true);
          IF $1 IS NULL THEN
            DELETE FROM inbound_mail AS m
             WHERE m.org_id IS NULL
               AND (lower(m.sender) = lower($3) OR lower(m.mail_from) = lower($3));
          ELSE
            IF $1 IS DISTINCT FROM {APP_ORG} THEN
              RAISE EXCEPTION 'andai_erase_inbound: the org must be the transaction''s org'
                USING ERRCODE = 'insufficient_privilege';
            END IF;
            DELETE FROM inbound_mail AS m
             WHERE m.org_id = $1
               AND (m.resolved_user_id = $2
                    OR lower(m.sender) = lower($3)
                    OR lower(m.mail_from) = lower($3));
          END IF;
          GET DIAGNOSTICS deleted = ROW_COUNT;
          PERFORM set_config('{ERASING}', 'off', true);
          RETURN deleted;
        END
        $$
        """
    )
    _hand_to_scan(ERASE_INBOUND)


def downgrade() -> None:
    for function in reversed(FUNCTIONS):
        op.execute(f"DROP FUNCTION {function}")
    op.execute("DROP POLICY scan_erase ON inbound_mail")
    op.execute(
        f"REVOKE SELECT ({', '.join(SCAN_SELECT_INBOUND)}) ON inbound_mail FROM {SCAN_ROLE}"
    )
    op.drop_table("erasure_requests")
    op.execute(
        f"""
        DO $$
        BEGIN
          UPDATE users SET status = 'disabled' WHERE status = 'erased';
          BEGIN
            DELETE FROM users WHERE id = '{TOMBSTONE_USER_ID}';
          EXCEPTION WHEN foreign_key_violation THEN
            NULL;  -- rows still point at it: it stays, as a disabled user
          END;
        END
        $$
        """
    )
    _user_status_check(USER_STATUSES_BEFORE)
