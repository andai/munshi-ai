"""The notetaker mailbox's store: `inbound_mail`, and the functions andai-intake writes it through.

* `inbound_mail` holds one row per message andai-intake accepted over SMTP: the raw bytes exactly as
  received (`raw`, NULL once purged), their sha256, the Message-ID, the envelope (`mail_from`,
  `rcpt_to`), the peer address and HELO, and the processing state (`status`: `received |
  processing | done | quarantined | ignored`, `attempts`, `lease_until`, `last_error`,
  `processed_at`). Processing fills in the SPEC `InboundInvite` result: `outcome`,
  `quarantine_reason`, `ignore_reason`, the attributed `org_id` and `resolved_user_id`, the
  verified `sender` and `sender_auth`, an `auth_summary` of the checks and the subject trimmed to
  120 characters (`subject_trim`). CHECK constraints keep the state consistent: an open row has no
  outcome, a quarantined row has its reason, a row in processing has a lease.
* FORCE ROW LEVEL SECURITY. `andai_app` may SELECT every column except `raw`, and only rows whose
  `org_id` is its `app.org_id`: a row nobody has attributed (`org_id IS NULL`) is visible to no
  org. `andai_app` may not INSERT, UPDATE or DELETE.
* Five functions, `SECURITY DEFINER`, owned by the NOLOGIN role `andai_scan`, executable by
  `andai_app` only (like 0005 and 0010). `andai_scan` gets column grants on exactly what they read
  and write, DELETE (its only table-level privilege anywhere, since DELETE has no column form), and
  role-scoped policies; `scan_purge` lets it delete only rows received at least a day ago.
  - `andai_store_inbound(raw, mail_from, rcpt_to, peer_ip, helo, message_id)` inserts a received
    message and returns `(id, duplicate)`. The same raw bytes stored within the last 24 hours are
    not stored again: the earlier row's id comes back with `duplicate = true`. A transaction-scoped
    advisory lock on the digest makes two concurrent deliveries of the same bytes store one row.
  - `andai_claim_inbound(max)` hands the processor at most `max` messages that still have their
    raw bytes and are `received`, or `processing` with an expired lease (a processor that died).
    Each is taken `FOR UPDATE SKIP LOCKED`, set `processing` with a 5-minute lease, and its
    `attempts` counted up, so a message that kills its processor is still counted.
  - `andai_finish_inbound(id, status, outcome, quarantine_reason, ignore_reason, org_id, user_id,
    sender, sender_auth, auth_summary, subject_trim)` records the result of a message in
    processing (`status` `done`, `quarantined` or `ignored`), sets `processed_at` and clears the
    lease. Returns whether the row was in processing.
  - `andai_release_inbound(id, error)` puts a message in processing back to `received` (lease
    cleared, `last_error` kept, at most 200 characters) so it is claimed again.
  - `andai_purge_inbound(raw_days, row_days)` nulls the raw bytes of messages received at least
    `raw_days` ago and deletes the rows received at least `row_days` ago (D-58: 7 and 30), and
    returns both counts.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "andai_app"
SCAN_ROLE = "andai_scan"
APP_ORG = "NULLIF(current_setting('app.org_id', true), '')::uuid"

STORE = "andai_store_inbound(bytea, text, text[], inet, text, text)"
CLAIM = "andai_claim_inbound(integer)"
FINISH = (
    "andai_finish_inbound(uuid, text, text, text, text, uuid, uuid, text, text, jsonb, text)"
)
RELEASE = "andai_release_inbound(uuid, text)"
PURGE = "andai_purge_inbound(integer, integer)"
FUNCTIONS = (STORE, CLAIM, FINISH, RELEASE, PURGE)

STATUSES = ("received", "processing", "done", "quarantined", "ignored")
FINAL_STATUSES = ("done", "quarantined", "ignored")
OUTCOMES = ("scheduled", "updated", "cancelled", "joined_now", "quarantined", "ignored")
QUARANTINE_REASONS = (
    "sender_unverified",
    "unknown_user",
    "no_meeting_link",
    "parse_error",
    "rate_limited",
)
SENDER_AUTH = ("dmarc_pass", "arc_trusted", "fail")
SUBJECT_MAX = 120
ERROR_MAX = 200
MESSAGE_ID_MAX = 998
DEDUP_WINDOW = "24 hours"
LEASE = "5 minutes"

COLUMNS = (
    "id",
    "received_at",
    "message_id",
    "raw_sha256",
    "raw",
    "mail_from",
    "rcpt_to",
    "peer_ip",
    "helo",
    "status",
    "outcome",
    "quarantine_reason",
    "ignore_reason",
    "org_id",
    "resolved_user_id",
    "sender",
    "sender_auth",
    "auth_summary",
    "subject_trim",
    "attempts",
    "lease_until",
    "last_error",
    "processed_at",
)
APP_SELECT = tuple(c for c in COLUMNS if c != "raw")
SCAN_INSERT = ("raw", "raw_sha256", "message_id", "mail_from", "rcpt_to", "peer_ip", "helo")
SCAN_SELECT = (
    "id",
    "received_at",
    "raw_sha256",
    "raw",
    "message_id",
    "mail_from",
    "rcpt_to",
    "peer_ip",
    "helo",
    "status",
    "attempts",
    "lease_until",
)
SCAN_UPDATE = (
    "raw",
    "status",
    "attempts",
    "lease_until",
    "last_error",
    "outcome",
    "quarantine_reason",
    "ignore_reason",
    "org_id",
    "resolved_user_id",
    "sender",
    "sender_auth",
    "auth_summary",
    "subject_trim",
    "processed_at",
)
# (policy, command, USING, WITH CHECK) for andai_scan, the functions' owner.
SCAN_POLICIES: tuple[tuple[str, str, str | None, str | None], ...] = (
    ("scan_read", "SELECT", "true", None),
    ("scan_insert", "INSERT", None, "true"),
    ("scan_update", "UPDATE", "true", "true"),
    ("scan_purge", "DELETE", "received_at <= now() - interval '1 day'", None),
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _hand_to_scan(function: str) -> None:
    op.execute(f"GRANT CREATE ON SCHEMA public TO {SCAN_ROLE}")
    op.execute(f"ALTER FUNCTION {function} OWNER TO {SCAN_ROLE}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {SCAN_ROLE}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {function} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO {APP_ROLE}")


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
        "inbound_mail",
        sa.Column(
            "id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        _ts("received_at", nullable=False, now=True),
        sa.Column("message_id", sa.Text, nullable=True),
        sa.Column("raw_sha256", sa.Text, nullable=False),
        sa.Column("raw", sa.LargeBinary, nullable=True),
        sa.Column("mail_from", sa.Text, nullable=False),
        sa.Column("rcpt_to", pg.ARRAY(sa.Text), nullable=False),
        sa.Column("peer_ip", pg.INET, nullable=True),
        sa.Column("helo", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'received'")),
        sa.Column("outcome", sa.Text, nullable=True),
        sa.Column("quarantine_reason", sa.Text, nullable=True),
        sa.Column("ignore_reason", sa.Text, nullable=True),
        sa.Column(
            "org_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE", name="fk_inbound_mail_org_id_orgs"),
            nullable=True,
        ),
        sa.Column(
            "resolved_user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey(
                "users.id", ondelete="SET NULL", name="fk_inbound_mail_resolved_user_id_users"
            ),
            nullable=True,
        ),
        sa.Column("sender", sa.Text, nullable=True),
        sa.Column("sender_auth", sa.Text, nullable=True),
        sa.Column("auth_summary", pg.JSONB, nullable=True),
        sa.Column("subject_trim", sa.Text, nullable=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        _ts("lease_until"),
        sa.Column("last_error", sa.Text, nullable=True),
        _ts("processed_at"),
        sa.CheckConstraint("raw_sha256 ~ '^[0-9a-f]{64}$'", name="ck_inbound_mail_raw_sha256"),
        sa.CheckConstraint(_in("status", STATUSES), name="ck_inbound_mail_status"),
        sa.CheckConstraint(
            f"outcome IS NULL OR {_in('outcome', OUTCOMES)}", name="ck_inbound_mail_outcome"
        ),
        sa.CheckConstraint(
            f"quarantine_reason IS NULL OR {_in('quarantine_reason', QUARANTINE_REASONS)}",
            name="ck_inbound_mail_quarantine_reason",
        ),
        sa.CheckConstraint(
            f"sender_auth IS NULL OR {_in('sender_auth', SENDER_AUTH)}",
            name="ck_inbound_mail_sender_auth",
        ),
        sa.CheckConstraint(
            f"char_length(subject_trim) <= {SUBJECT_MAX}",
            name="ck_inbound_mail_subject_trim_length",
        ),
        sa.CheckConstraint(
            "(status IN ('received', 'processing')) = (outcome IS NULL)",
            name="ck_inbound_mail_open_has_no_outcome",
        ),
        sa.CheckConstraint(
            "(status = 'quarantined') = (outcome IS NOT DISTINCT FROM 'quarantined')",
            name="ck_inbound_mail_quarantined_outcome",
        ),
        sa.CheckConstraint(
            "(status = 'ignored') = (outcome IS NOT DISTINCT FROM 'ignored')",
            name="ck_inbound_mail_ignored_outcome",
        ),
        sa.CheckConstraint(
            "(status = 'quarantined') = (quarantine_reason IS NOT NULL)",
            name="ck_inbound_mail_quarantined_has_reason",
        ),
        sa.CheckConstraint(
            "ignore_reason IS NULL OR status = 'ignored'",
            name="ck_inbound_mail_ignore_reason_when_ignored",
        ),
        sa.CheckConstraint(
            "(status = 'processing') = (lease_until IS NOT NULL)",
            name="ck_inbound_mail_processing_has_lease",
        ),
    )
    op.create_index(
        "ix_inbound_mail_raw_sha256_received_at", "inbound_mail", ["raw_sha256", "received_at"]
    )
    op.create_index("ix_inbound_mail_received_at", "inbound_mail", ["received_at"])
    op.create_index(
        "ix_inbound_mail_open",
        "inbound_mail",
        ["received_at"],
        postgresql_where=sa.text("status IN ('received', 'processing')"),
    )
    op.create_index(
        "ix_inbound_mail_org_id_status_received_at",
        "inbound_mail",
        ["org_id", "status", "received_at"],
    )

    # ── row-level security: the app role reads its own org's attributed rows, never raw ──
    op.execute("ALTER TABLE inbound_mail ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE inbound_mail FORCE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT ({', '.join(APP_SELECT)}) ON inbound_mail TO {APP_ROLE}")
    op.execute(
        "CREATE POLICY org_read ON inbound_mail AS PERMISSIVE FOR SELECT"
        f" USING (org_id = {APP_ORG})"
    )

    # ── andai_scan: exactly what the functions touch ──
    op.execute(f"GRANT INSERT ({', '.join(SCAN_INSERT)}) ON inbound_mail TO {SCAN_ROLE}")
    op.execute(f"GRANT SELECT ({', '.join(SCAN_SELECT)}) ON inbound_mail TO {SCAN_ROLE}")
    op.execute(f"GRANT UPDATE ({', '.join(SCAN_UPDATE)}) ON inbound_mail TO {SCAN_ROLE}")
    # DELETE cannot be granted per column: it is the scan role's only table-level privilege, and
    # its policy lets it remove nothing received in the last day (the purge keeps rows >= 1 day).
    op.execute(f"GRANT DELETE ON inbound_mail TO {SCAN_ROLE}")
    for name, command, using, check in SCAN_POLICIES:
        sql = f"CREATE POLICY {name} ON inbound_mail AS PERMISSIVE FOR {command} TO {SCAN_ROLE}"
        if using is not None:
            sql += f" USING ({using})"
        if check is not None:
            sql += f" WITH CHECK ({check})"
        op.execute(sql)

    op.execute(
        f"""
        CREATE FUNCTION {STORE}
        RETURNS TABLE (id uuid, duplicate boolean)
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        DECLARE
          digest text;
          earlier uuid;
        BEGIN
          IF $1 IS NULL OR octet_length($1) = 0 THEN
            RAISE EXCEPTION 'andai_store_inbound: empty message';
          END IF;
          IF $3 IS NULL OR cardinality($3) = 0 THEN
            RAISE EXCEPTION 'andai_store_inbound: no recipient';
          END IF;
          digest := encode(sha256($1), 'hex');
          PERFORM pg_advisory_xact_lock(hashtextextended('andai_inbound:' || digest, 0));
          SELECT m.id INTO earlier
            FROM inbound_mail m
           WHERE m.raw_sha256 = digest
             AND m.received_at > now() - interval '{DEDUP_WINDOW}'
           ORDER BY m.received_at DESC
           LIMIT 1;
          IF earlier IS NOT NULL THEN
            RETURN QUERY SELECT earlier, true;
            RETURN;
          END IF;
          RETURN QUERY
            INSERT INTO inbound_mail AS m
                   (raw, raw_sha256, mail_from, rcpt_to, peer_ip, helo, message_id)
            VALUES ($1, digest, coalesce($2, ''), $3, $4, $5, left($6, {MESSAGE_ID_MAX}))
            RETURNING m.id, false;
        END
        $$
        """
    )
    _hand_to_scan(STORE)
    op.execute(
        f"""
        CREATE FUNCTION {CLAIM}
        RETURNS TABLE (id uuid, received_at timestamptz, raw bytea, mail_from text,
                       rcpt_to text[], peer_ip inet, helo text, message_id text, attempts integer)
        LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
          WITH due AS (
            SELECT m.id
              FROM inbound_mail m
             WHERE m.raw IS NOT NULL
               AND (m.status = 'received'
                    OR (m.status = 'processing' AND m.lease_until <= now()))
             ORDER BY m.received_at, m.id
             LIMIT greatest($1, 0)
             FOR UPDATE SKIP LOCKED
          )
          UPDATE inbound_mail AS t
             SET status = 'processing',
                 lease_until = now() + interval '{LEASE}',
                 attempts = t.attempts + 1
            FROM due
           WHERE t.id = due.id
          RETURNING t.id, t.received_at, t.raw, t.mail_from, t.rcpt_to, t.peer_ip, t.helo,
                    t.message_id, t.attempts
        $$
        """
    )
    _hand_to_scan(CLAIM)
    op.execute(
        f"""
        CREATE FUNCTION {FINISH}
        RETURNS boolean
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        DECLARE
          changed integer;
        BEGIN
          IF $2 IS NULL OR NOT {_in('$2', FINAL_STATUSES)} THEN
            RAISE EXCEPTION 'andai_finish_inbound: status must be one of {", ".join(FINAL_STATUSES)}';
          END IF;
          UPDATE inbound_mail AS m
             SET status = $2,
                 outcome = $3,
                 quarantine_reason = $4,
                 ignore_reason = $5,
                 org_id = $6,
                 resolved_user_id = $7,
                 sender = $8,
                 sender_auth = $9,
                 auth_summary = $10,
                 subject_trim = left($11, {SUBJECT_MAX}),
                 processed_at = now(),
                 lease_until = NULL
           WHERE m.id = $1 AND m.status = 'processing';
          GET DIAGNOSTICS changed = ROW_COUNT;
          RETURN changed = 1;
        END
        $$
        """
    )
    _hand_to_scan(FINISH)
    op.execute(
        f"""
        CREATE FUNCTION {RELEASE}
        RETURNS boolean
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        DECLARE
          changed integer;
        BEGIN
          UPDATE inbound_mail AS m
             SET status = 'received',
                 lease_until = NULL,
                 last_error = left($2, {ERROR_MAX})
           WHERE m.id = $1 AND m.status = 'processing';
          GET DIAGNOSTICS changed = ROW_COUNT;
          RETURN changed = 1;
        END
        $$
        """
    )
    _hand_to_scan(RELEASE)
    op.execute(
        f"""
        CREATE FUNCTION {PURGE}
        RETURNS TABLE (raw_purged bigint, rows_deleted bigint)
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        DECLARE
          purged bigint;
          deleted bigint;
        BEGIN
          IF $1 IS NULL OR $2 IS NULL OR $1 < 1 OR $2 < $1 THEN
            RAISE EXCEPTION 'andai_purge_inbound: need 1 <= raw_days <= row_days';
          END IF;
          DELETE FROM inbound_mail AS m
           WHERE m.received_at <= now() - make_interval(days => $2);
          GET DIAGNOSTICS deleted = ROW_COUNT;
          UPDATE inbound_mail AS m
             SET raw = NULL
           WHERE m.raw IS NOT NULL
             AND m.received_at <= now() - make_interval(days => $1);
          GET DIAGNOSTICS purged = ROW_COUNT;
          RETURN QUERY SELECT purged, deleted;
        END
        $$
        """
    )
    _hand_to_scan(PURGE)


def downgrade() -> None:
    for function in reversed(FUNCTIONS):
        op.execute(f"DROP FUNCTION {function}")
    op.drop_table("inbound_mail")
