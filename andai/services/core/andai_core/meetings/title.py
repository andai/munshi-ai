"""A meeting's title and its search form.

`set_title` is the only writer of `meetings.title`: it stores the title (trimmed; blank means none)
and `title_search = fold(title)` together, so search always reads the fold of the title shown.
Tests, seeds and the calendar sync that names meetings (Phase 7) all write through it.
"""

from __future__ import annotations

from sqlalchemy.orm import Session as Tx

from andai_core.db.models import Meeting
from andai_core.text import fold


def set_title(tx: Tx, meeting: Meeting, title: str | None) -> None:
    """Give `meeting` this title (None or blank clears it) and the matching search text; the
    meeting is added to `tx` if it is new."""
    cleaned = (title or "").strip()
    meeting.title = cleaned or None
    meeting.title_search = (fold(cleaned) or None) if cleaned else None
    tx.add(meeting)


__all__ = ["set_title"]
