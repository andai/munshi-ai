"""Meeting shares inside an org, and the indexes the meeting list reads through.

* `meeting_shares` — one grant of read access to a meeting: to a user (`grantee_type = 'user'`) or
  to a team (`'team'`, every member of the team at the time of each read). At most one grant per
  (meeting, grantee). The grantee's id is checked against the caller's org in the transaction that
  grants it (`meetings/shares.py`); the table is org-scoped under FORCE ROW LEVEL SECURITY with the
  usual `org_isolation` policy and CRUD grants to `andai_app`, and cascades with its meeting.
  `(org_id, grantee_type, grantee_id)` serves "shared with me".
* `meetings` gets two list indexes:
  - `(org_id, status, coalesce(started_at, created_at) DESC, id DESC)` for the Live / Upcoming /
    Past sections in their order (newest first, the id breaking ties);
  - `(org_id, requested_by)` for "requested by me" and the requester arm of the access predicate.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "andai_app"
APP_ORG = "NULLIF(current_setting('app.org_id', true), '')::uuid"
GRANTEE_TYPES = ("user", "team")


def upgrade() -> None:
    op.create_table(
        "meeting_shares",
        sa.Column(
            "id", pg.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("org_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("meeting_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("grantee_type", sa.Text, nullable=False),
        sa.Column("grantee_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("granted_by", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_meeting_shares"),
        sa.ForeignKeyConstraint(
            ["meeting_id", "org_id"],
            ["meetings.id", "meetings.org_id"],
            ondelete="CASCADE",
            name="fk_meeting_shares_meeting_id_org_id_meetings",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_meeting_shares_granted_by_users",
        ),
        sa.UniqueConstraint(
            "meeting_id",
            "grantee_type",
            "grantee_id",
            name="uq_meeting_shares_meeting_id_grantee_type_grantee_id",
        ),
        sa.CheckConstraint(
            f"grantee_type IN ({', '.join(repr(v) for v in GRANTEE_TYPES)})",
            name="ck_meeting_shares_grantee_type",
        ),
    )
    op.create_index(
        "ix_meeting_shares_org_id_grantee_type_grantee_id",
        "meeting_shares",
        ["org_id", "grantee_type", "grantee_id"],
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON meeting_shares TO {APP_ROLE}")
    op.execute("ALTER TABLE meeting_shares ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE meeting_shares FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY org_isolation ON meeting_shares AS PERMISSIVE FOR ALL"
        f" USING (org_id = {APP_ORG}) WITH CHECK (org_id = {APP_ORG})"
    )

    op.execute(
        "CREATE INDEX ix_meetings_org_id_status_sort_time ON meetings"
        " (org_id, status, (coalesce(started_at, created_at)) DESC, id DESC)"
    )
    op.create_index("ix_meetings_org_id_requested_by", "meetings", ["org_id", "requested_by"])


def downgrade() -> None:
    op.drop_index("ix_meetings_org_id_requested_by", table_name="meetings")
    op.drop_index("ix_meetings_org_id_status_sort_time", table_name="meetings")
    op.drop_table("meeting_shares")
