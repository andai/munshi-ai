"""``transcriptions.created_at`` is the time a segment first reached the collector, and the
transcript read serves it — so andai's ASR-lag histogram (write time − spoken end) gets real
values instead of the time of the last flush.

This venv has no SQLAlchemy, so the SQL adapter's two methods run against small stand-ins: the
upsert's own SQL text is executed on SQLite (which has the same ``INSERT … ON CONFLICT (…) WHERE
… DO UPDATE`` semantics as Postgres for this statement), and the transcript read's DB half gets a
row object shaped like the ORM's."""
from __future__ import annotations

import sqlite3
import sys
import types
from datetime import datetime, timezone

import pytest

from meeting_api.collector.adapters import SqlAlchemyTranscriptStore, _segment_to_api

SCHEMA = """
CREATE TABLE transcriptions (
    id INTEGER PRIMARY KEY, meeting_id INTEGER NOT NULL, start_time REAL, end_time REAL,
    text TEXT, speaker TEXT, language TEXT, session_uid TEXT, segment_id TEXT, created_at TEXT
);
CREATE UNIQUE INDEX ix_transcription_meeting_segment
    ON transcriptions (meeting_id, segment_id) WHERE segment_id IS NOT NULL;
"""


def _sqlite_session_factory(conn: sqlite3.Connection):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, statement, params=None):
            conn.execute(str(statement), {
                k: (v.isoformat(sep=" ") if isinstance(v, datetime) else v)
                for k, v in (params or {}).items()
            })

        async def commit(self):
            conn.commit()

    return Session


@pytest.fixture
def sql_store(monkeypatch):
    """The SQL adapter over SQLite, with ``sqlalchemy.text`` standing in as the identity."""
    monkeypatch.setitem(sys.modules, "sqlalchemy", types.SimpleNamespace(text=lambda sql: sql))
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    yield SqlAlchemyTranscriptStore(_sqlite_session_factory(conn)), conn
    conn.close()


def _seg(text: str, end: float, first_seen_at: str | None) -> dict:
    seg = {"segment_id": "s1", "start": 1.0, "end": end, "text": text, "speaker": "Asha",
           "language": "hi", "completed": True}
    if first_seen_at is not None:
        seg["first_seen_at"] = first_seen_at
    return seg


def _row(conn) -> tuple:
    return conn.execute(
        "SELECT text, end_time, created_at FROM transcriptions WHERE segment_id = 's1'"
    ).fetchone()


async def test_a_reflush_keeps_created_at_and_updates_text_and_end(sql_store):
    store, conn = sql_store
    await store.upsert_segments(5, [_seg("namaste", 2.0, "2026-11-17T10:00:00Z")])
    await store.upsert_segments(5, [_seg("namaste, sab log", 3.5, "2026-11-17T10:05:00Z")])

    text, end, created = _row(conn)
    assert (text, end) == ("namaste, sab log", 3.5)
    assert created == "2026-11-17 10:00:00"                  # naive UTC, as the column holds it


async def test_a_segment_without_first_seen_at_gets_the_flush_time(sql_store):
    """Segments written to the hash before first_seen_at existed still get a created_at."""
    store, conn = sql_store
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    await store.upsert_segments(5, [_seg("purana", 2.0, None)])
    created = datetime.fromisoformat(_row(conn)[2])
    assert before <= created <= datetime.now(timezone.utc).replace(tzinfo=None)


async def test_an_offset_first_seen_at_is_stored_as_utc(sql_store):
    store, conn = sql_store
    await store.upsert_segments(5, [_seg("namaste", 2.0, "2026-11-17T15:30:00+05:30")])
    assert _row(conn)[2] == "2026-11-17 10:00:00"


# ── the transcript read ─────────────────────────────────────────────────────────────────────────


class _Column:
    def __eq__(self, other):  # Transcription.meeting_id == meeting.id
        return ("eq", other)


async def test_the_pg_transcript_read_serves_created_at_as_iso_utc(monkeypatch):
    class _Select:
        def where(self, *_):
            return self

    monkeypatch.setitem(sys.modules, "sqlalchemy",
                        types.SimpleNamespace(select=lambda *_: _Select()))
    monkeypatch.setitem(sys.modules, "meeting_api.collector.models",
                        types.SimpleNamespace(Transcription=types.SimpleNamespace(meeting_id=_Column())))
    row = types.SimpleNamespace(
        start_time=1.0, end_time=2.0, text="namaste", language="hi", speaker="Asha",
        segment_id="s1", created_at=datetime(2026, 11, 17, 10, 0, 0),     # naive UTC column
    )

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [row]

    class _Db:
        async def execute(self, *_):
            return _Result()

    meeting = types.SimpleNamespace(
        id=5, data={}, platform="google_meet", platform_specific_id="abc-defg-hij",
        status="completed", start_time=None, end_time=None, created_at=None,
    )
    _, seg_by_id, _ = await SqlAlchemyTranscriptStore(None)._transcript_pg_part(_Db(), meeting)

    assert seg_by_id["s1"]["created_at"] == "2026-11-17T10:00:00Z"


def test_a_live_segment_serves_first_seen_at_as_created_at():
    seg = _segment_to_api({"start": 1.0, "end": 2.0, "text": "abhi", "segment_id": "s2",
                           "first_seen_at": "2026-11-17T10:00:00Z",
                           "updated_at": "2026-11-17T10:00:09Z"})
    assert seg["created_at"] == "2026-11-17T10:00:00Z"
    assert "first_seen_at" not in seg and "updated_at" not in seg
