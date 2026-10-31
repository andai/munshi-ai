"""Minutes of meeting (read model) and keyword search over titles, transcripts and minutes.

* `pg_trgm` (a trusted contrib extension, so the owner role may create it) for spelling variants
  of titles and minutes.
* `mom_documents` holds one row per version of a meeting's minutes:
  - `body` is the SPEC `MomDocument` (JSON), with `meetingId` and `version` equal to the row's;
  - `search_text` is the fold (`andai_core.text.fold`) of its summary, decisions, action items,
    topics and risks, the form search reads, and `search_tsv` its stored `tsvector`.

  `(meeting_id, version)` is the key. The table is org-scoped under FORCE ROW LEVEL SECURITY with
  the usual `org_isolation` policy and cascades with its meeting. `andai_app` may SELECT and INSERT
  only: a version is never changed, an edit is a new version.
* `meetings.title_search` is the fold of `meetings.title`, written together with it by
  `meetings/title.py`'s `set_title`, the only title writer.
* `transcript_segments.search_tsv` is `to_tsvector('simple', search_text)`, generated and stored.
  The app role searches under FORCE ROW LEVEL SECURITY, where Postgres evaluates a condition whose
  operator is not LEAKPROOF (`@@`, `<<%`) only after the row's policy check, row by row, and never
  through an index. Search therefore reads every segment of the org, and a stored vector is what
  keeps that cheap: no text is parsed per row.
* GIN indexes: whole words (`tsvector`) on all three texts, and `gin_trgm_ops` on the two that are
  searched for spelling variants, titles and minutes. They serve a reader that is not under RLS
  (superuser, maintenance); the app role's plans scan instead.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "andai_app"
APP_ORG = "NULLIF(current_setting('app.org_id', true), '')::uuid"
OUTPUT_LANGUAGES = ("en", "hi", "hinglish")

TSV = "to_tsvector('simple'::regconfig, {})"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.add_column("meetings", sa.Column("title_search", sa.Text, nullable=True))

    op.create_table(
        "mom_documents",
        sa.Column("meeting_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("org_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("template_id", sa.Text, nullable=False),
        sa.Column("output_language", sa.Text, nullable=False),
        sa.Column("body", pg.JSONB, nullable=False),
        sa.Column("search_text", sa.Text, nullable=False),
        sa.Column(
            "search_tsv",
            pg.TSVECTOR,
            sa.Computed(TSV.format("search_text"), persisted=True),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("meeting_id", "version", name="pk_mom_documents"),
        sa.ForeignKeyConstraint(
            ["meeting_id", "org_id"],
            ["meetings.id", "meetings.org_id"],
            ondelete="CASCADE",
            name="fk_mom_documents_meeting_id_org_id_meetings",
        ),
        sa.CheckConstraint("version >= 1", name="ck_mom_documents_version"),
        sa.CheckConstraint(
            f"output_language IN ({', '.join(repr(v) for v in OUTPUT_LANGUAGES)})",
            name="ck_mom_documents_output_language",
        ),
    )
    op.execute(f"GRANT SELECT, INSERT ON mom_documents TO {APP_ROLE}")
    op.execute("ALTER TABLE mom_documents ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE mom_documents FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY org_isolation ON mom_documents AS PERMISSIVE FOR ALL"
        f" USING (org_id = {APP_ORG}) WITH CHECK (org_id = {APP_ORG})"
    )

    op.add_column(
        "transcript_segments",
        sa.Column(
            "search_tsv",
            pg.TSVECTOR,
            sa.Computed(TSV.format("search_text"), persisted=True),
            nullable=False,
        ),
    )

    op.execute(
        "CREATE INDEX ix_transcript_segments_search_tsv ON transcript_segments"
        " USING gin (search_tsv)"
    )
    op.execute("CREATE INDEX ix_mom_documents_search_tsv ON mom_documents USING gin (search_tsv)")
    op.execute(
        "CREATE INDEX ix_mom_documents_search_text_trgm ON mom_documents"
        " USING gin (search_text gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_meetings_title_search_fts ON meetings"
        f" USING gin ({TSV.format('title_search')})"
    )
    op.execute(
        "CREATE INDEX ix_meetings_title_search_trgm ON meetings"
        " USING gin (title_search gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_meetings_title_search_trgm")
    op.execute("DROP INDEX ix_meetings_title_search_fts")
    op.execute("DROP INDEX ix_transcript_segments_search_tsv")
    op.drop_column("transcript_segments", "search_tsv")
    op.drop_table("mom_documents")
    op.drop_column("meetings", "title_search")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
