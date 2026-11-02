"""Stored Devanagari text re-normalised with the current normaliser.

The normaliser's medial schwa rule keeps the inherent `a` before a conjunct (`नमस्ते` → `namaste`,
`कुलकर्णी` → `kulkarni`). Search folds a query with the current rule, so every stored form made
from Devanagari text is recomputed here with `andai_core.text`:

* `transcript_segments.text_canonical` and `search_text` (`search_tsv` follows, generated), for
  segments whose script is `deva` or `mixed`;
* `mom_documents.search_text` (`meetings.mom.search_text`), for bodies with Devanagari letters;
* `meetings.title_search`, for titles with Devanagari letters.

Only rows whose stored form changes are written. FORCE ROW LEVEL SECURITY binds the owner too; it
is lifted for these statements only, inside the migration's transaction. The downgrade leaves the
forms as they are: they are the current normaliser's output, and the old rule is not kept.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from andai_core.meetings.mom import search_text
from andai_core.text import canonical, fold

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("transcript_segments", "mom_documents", "meetings")
DEVANAGARI = "'[ऀ-ॿ]'"


def upgrade() -> None:
    bind = op.get_bind()
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")

    segments = []
    for row in bind.execute(
        sa.text(
            "SELECT meeting_id, id, text_raw, text_canonical, search_text"
            " FROM transcript_segments WHERE script IN ('deva', 'mixed')"
        )
    ):
        roman = canonical(row.text_raw)
        folded = fold(roman)
        if (roman, folded) != (row.text_canonical, row.search_text):
            segments.append({"m": row.meeting_id, "i": row.id, "c": roman, "s": folded})
    if segments:
        bind.execute(
            sa.text(
                "UPDATE transcript_segments SET text_canonical = :c, search_text = :s"
                " WHERE meeting_id = :m AND id = :i"
            ),
            segments,
        )

    documents = []
    for row in bind.execute(
        sa.text(
            "SELECT meeting_id, version, body, search_text FROM mom_documents"
            f" WHERE body::text ~ {DEVANAGARI}"
        )
    ):
        try:
            folded = search_text(row.body)
        except ValueError:  # a body the current document model does not read keeps its form
            continue
        if folded != row.search_text:
            documents.append({"m": row.meeting_id, "v": row.version, "s": folded})
    if documents:
        bind.execute(
            sa.text(
                "UPDATE mom_documents SET search_text = :s WHERE meeting_id = :m AND version = :v"
            ),
            documents,
        )

    titles = []
    for row in bind.execute(
        sa.text(f"SELECT id, title, title_search FROM meetings WHERE title ~ {DEVANAGARI}")
    ):
        folded = fold(row.title) or None
        if folded != row.title_search:
            titles.append({"m": row.id, "s": folded})
    if titles:
        bind.execute(sa.text("UPDATE meetings SET title_search = :s WHERE id = :m"), titles)

    for table in TABLES:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    pass
