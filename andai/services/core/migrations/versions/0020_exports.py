"""DPDP data export (OPS-02): the request table andai-core writes and andai-worker builds, and the
functions andai-worker claims and expires requests of every org through.

* `export_requests`: one row per copy of a person's data in one org. `subject_user_id` is whose data
  it is, `requested_by` who asked (the person themselves for `source = self`, an owner or admin of
  the org for `org_admin`); both are foreign keys to `users` (the erasure registry deletes the
  subject's rows and re-points `requested_by` at the tombstone user). `state` (`pending`,
  `running`, `ready`, `failed`, `expired`), `attempts`, `next_at`, `lease_until`, `error` (a
  closed set: `too_large`, `not_a_member` when the person left the org before the build,
  `internal_error`). A ready bundle's JSON part is sealed with the envelope keyring
  (`bundle_ciphertext` + `key_id`, AAD `export:<org>:<request>`); the recordings
  are not copied: `recordings` lists each one's platform ids, file name, size, SHA-256 and media
  type, and the download streams them from the platform. `bundle_bytes` is the size of every file
  of the bundle. `created_at`, `ready_at`, `expires_at`. At most one open (`pending`/`running`)
  request per (org, person). FORCE ROW LEVEL SECURITY on `org_id` (`org_isolation`, like 0002);
  `andai_app` may SELECT, INSERT, UPDATE and DELETE (the erasure scrub deletes the subject's rows).
* Functions, `SECURITY DEFINER`, owned by the NOLOGIN role `andai_scan`, executable by `andai_app`
  only (like 0009 and 0019):
  - `andai_claim_exports(max, at)` hands andai-worker at most `max` open requests of any org that
    are due (`next_at` passed, lease expired) as `(org_id, request_id, attempts)`, taken
    `FOR UPDATE SKIP LOCKED`, set `running` and leased for 10 minutes doubling with `attempts` up to
    an hour (a build streams every recording once).
  - `andai_expire_exports(at)` sets every `ready` request whose `expires_at` has passed, in every
    org, to `expired` and clears its `bundle_ciphertext` and `key_id`; returns the count.
  `andai_scan` gets SELECT and UPDATE column grants for exactly that, and role-scoped policies
  `scan_read`, `scan_claim` (open → `running`) and `scan_expire` (`ready` → `expired`).

The downgrade drops the functions and the table.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "andai_app"
SCAN_ROLE = "andai_scan"
APP_ORG = "NULLIF(current_setting('app.org_id', true), '')::uuid"

SOURCES = ("self", "org_admin")
STATES = ("pending", "running", "ready", "failed", "expired")
OPEN_STATES = ("pending", "running")
ERRORS = ("too_large", "not_a_member", "internal_error")

CLAIM = "andai_claim_exports(integer, timestamptz)"
EXPIRE = "andai_expire_exports(timestamptz)"
FUNCTIONS = (CLAIM, EXPIRE)

LEASE_BASE_S = 600
LEASE_DOUBLINGS = 3
LEASE_MAX = "1 hour"

SCAN_SELECT = (
    "id",
    "org_id",
    "state",
    "attempts",
    "next_at",
    "lease_until",
    "created_at",
    "expires_at",
)
SCAN_UPDATE = ("state", "lease_until", "bundle_ciphertext", "key_id")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


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

    op.create_table(
        "export_requests",
        sa.Column(
            "id", pg.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("org_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_user_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("requested_by", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("next_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("bundle_ciphertext", sa.Text, nullable=True),
        sa.Column("key_id", sa.Text, nullable=True),
        sa.Column("bundle_bytes", sa.BigInteger, nullable=True),
        sa.Column(
            "recordings", pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_export_requests"),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], ondelete="RESTRICT", name="fk_export_requests_org_id_orgs"
        ),
        sa.ForeignKeyConstraint(
            ["subject_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_export_requests_subject_user_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_export_requests_requested_by_users",
        ),
        sa.CheckConstraint(_in("source", SOURCES), name="source"),
        sa.CheckConstraint(_in("state", STATES), name="state"),
        sa.CheckConstraint(f"error IS NULL OR {_in('error', ERRORS)}", name="error"),
        sa.CheckConstraint("(state = 'failed') = (error IS NOT NULL)", name="failed_has_error"),
        sa.CheckConstraint(
            "(bundle_ciphertext IS NULL) = (key_id IS NULL)", name="sealed_with_key"
        ),
        sa.CheckConstraint(
            "state <> 'ready' OR (bundle_ciphertext IS NOT NULL AND expires_at IS NOT NULL)",
            name="ready_has_bundle",
        ),
        sa.CheckConstraint(
            "source <> 'self' OR requested_by = subject_user_id", name="self_is_subject"
        ),
    )
    op.create_index(
        "uq_export_requests_open",
        "export_requests",
        ["org_id", "subject_user_id"],
        unique=True,
        postgresql_where=sa.text(_in("state", OPEN_STATES)),
    )
    op.create_index(
        "ix_export_requests_due",
        "export_requests",
        ["next_at"],
        postgresql_where=sa.text(_in("state", OPEN_STATES)),
    )
    op.create_index(
        "ix_export_requests_expiry",
        "export_requests",
        ["expires_at"],
        postgresql_where=sa.text("state = 'ready'"),
    )
    op.create_index(
        "ix_export_requests_org_id_subject_user_id_created_at",
        "export_requests",
        ["org_id", "subject_user_id", "created_at"],
    )
    op.create_index(
        "ix_export_requests_org_id_requested_by", "export_requests", ["org_id", "requested_by"]
    )
    op.create_index(
        "ix_export_requests_org_id_created_at", "export_requests", ["org_id", "created_at", "id"]
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON export_requests TO {APP_ROLE}")
    op.execute("ALTER TABLE export_requests ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE export_requests FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY org_isolation ON export_requests AS PERMISSIVE FOR ALL"
        f" USING (org_id = {APP_ORG}) WITH CHECK (org_id = {APP_ORG})"
    )

    # ── andai_scan: exactly what the functions touch ──
    op.execute(f"GRANT SELECT ({', '.join(SCAN_SELECT)}) ON export_requests TO {SCAN_ROLE}")
    op.execute(f"GRANT UPDATE ({', '.join(SCAN_UPDATE)}) ON export_requests TO {SCAN_ROLE}")
    op.execute(
        f"CREATE POLICY scan_read ON export_requests AS PERMISSIVE FOR SELECT TO {SCAN_ROLE}"
        " USING (true)"
    )
    op.execute(
        f"CREATE POLICY scan_claim ON export_requests AS PERMISSIVE FOR UPDATE TO {SCAN_ROLE}"
        f" USING ({_in('state', OPEN_STATES)}) WITH CHECK (state = 'running')"
    )
    op.execute(
        f"CREATE POLICY scan_expire ON export_requests AS PERMISSIVE FOR UPDATE TO {SCAN_ROLE}"
        " USING (state = 'ready') WITH CHECK (state = 'expired')"
    )

    lease = (
        f"least(make_interval(secs => {LEASE_BASE_S}"
        f" * power(2, least(t.attempts, {LEASE_DOUBLINGS}))), interval '{LEASE_MAX}')"
    )
    op.execute(
        f"""
        CREATE FUNCTION {CLAIM}
        RETURNS TABLE (org_id uuid, request_id uuid, attempts integer)
        LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          WITH due AS (
            SELECT r.id
              FROM export_requests r
             WHERE r.state IN ('pending', 'running')
               AND (r.next_at IS NULL OR r.next_at <= $2)
               AND (r.lease_until IS NULL OR r.lease_until <= $2)
             ORDER BY r.next_at NULLS FIRST, r.created_at, r.id
             LIMIT greatest($1, 0)
             FOR UPDATE SKIP LOCKED
          )
          UPDATE export_requests AS t
             SET state = 'running',
                 lease_until = $2 + {lease}
            FROM due
           WHERE t.id = due.id
          RETURNING t.org_id, t.id, t.attempts
        $$
        """
    )
    _hand_to_scan(CLAIM)
    op.execute(
        f"""
        CREATE FUNCTION {EXPIRE}
        RETURNS bigint
        LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          WITH gone AS (
            UPDATE export_requests AS t
               SET state = 'expired',
                   bundle_ciphertext = NULL,
                   key_id = NULL
             WHERE t.state = 'ready'
               AND t.expires_at <= $1
            RETURNING t.id
          )
          SELECT count(*)::bigint FROM gone
        $$
        """
    )
    _hand_to_scan(EXPIRE)


def downgrade() -> None:
    for function in reversed(FUNCTIONS):
        op.execute(f"DROP FUNCTION {function}")
    op.drop_table("export_requests")
