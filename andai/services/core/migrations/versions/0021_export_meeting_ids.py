"""The meetings a copy of a person's data holds (OPS-02), so that deleting a meeting invalidates
every copy that holds it.

* `export_requests.meeting_ids uuid[] NOT NULL DEFAULT '{}'`: the ids of the meetings in the
  request's JSON part, written by andai-worker when it reads the person's data and again when the
  copy turns `ready`. A GIN index serves `meeting_ids @> ARRAY[meeting]` from the meeting purge.
  Copies made before this revision keep the empty default; the purge treats those conservatively
  (`andai_core.meetings.deletion`).
* The `error` set gains `meeting_deleted`: an open request whose meeting was deleted before its copy
  was stored fails with it, and the person can ask again.

The ids are meeting ids, not references to a person, so the erasure registry needs no rule for
them: the subject's requests are deleted with the subject.

The downgrade drops the column and its index and restores 0020's error set (a request that failed
`meeting_deleted` becomes `internal_error`).

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ERRORS_0020 = ("too_large", "not_a_member", "internal_error")
ERRORS = (*ERRORS_0020, "meeting_deleted")
INDEX = "ix_export_requests_meeting_ids"
# 0020 named it "error"; the models' naming convention made it this.
ERROR_CHECK = "ck_export_requests_error"


def _error_check(errors: tuple[str, ...]) -> str:
    return f"error IS NULL OR error IN ({', '.join(repr(e) for e in errors)})"


def upgrade() -> None:
    op.add_column(
        "export_requests",
        sa.Column(
            "meeting_ids",
            pg.ARRAY(pg.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
    )
    op.create_index(INDEX, "export_requests", ["meeting_ids"], postgresql_using="gin")
    op.drop_constraint(op.f(ERROR_CHECK), "export_requests", type_="check")
    op.create_check_constraint("error", "export_requests", _error_check(ERRORS))


def downgrade() -> None:
    # FORCE ROW LEVEL SECURITY binds the owner too; it is lifted for this one statement, inside
    # the migration's transaction, so the requests of every org are mapped.
    op.execute("ALTER TABLE export_requests NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE export_requests SET error = 'internal_error' WHERE error = 'meeting_deleted'"
    )
    op.execute("ALTER TABLE export_requests FORCE ROW LEVEL SECURITY")
    op.drop_constraint(op.f(ERROR_CHECK), "export_requests", type_="check")
    op.create_check_constraint("error", "export_requests", _error_check(ERRORS_0020))
    op.drop_index(INDEX, table_name="export_requests")
    op.drop_column("export_requests", "meeting_ids")
