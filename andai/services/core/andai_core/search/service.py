"""Keyword search over meeting titles, transcripts and minutes (PORT-05).

**What is matched.** A query is folded like the documents (`andai_core.text.fold`: Devanagari to
Roman Hinglish, lowercase, spelling variants merged), so `kal`, `KAAL` and `कल` are one query and
find segments spoken as `कल`. Three folded texts are searched: each stored transcript segment
(`transcript_segments.search_text`), the meeting title (`meetings.title_search`) and the latest
minutes version (`mom_documents.search_text`). A text matches when its words contain every query
word (`tsvector @@ websearch_to_tsquery('simple', folded query)`; the fold has removed any search
syntax, so the query is plain words). A title or the minutes also match when the query is a close
spelling variant of whole words of them (`pg_trgm` strict word similarity ≥ 0.5, the `<<%`
operator; the plain `<%` lets a match straddle words, so `kal` would match "fina*l ka*ren").

**Cost.** The app role reads under FORCE ROW LEVEL SECURITY, where Postgres checks each row's
policy before any condition whose operator is not LEAKPROOF, so `@@` and `<<%` never go through an
index: search reads every segment of the caller's org. Segments carry a stored `search_tsv`, so
that read parses no text (about 25 ms per 200,000 segments); a trigram test costs about 2 µs per
row, so it runs on titles and minutes only. Transcript spelling variants come from the fold.

**Access.** `meetings/access.accessible(actor)` is part of every arm of the SQL, before grouping,
ranking, counting and the page limit, so neither a result nor the total ever includes a meeting the
caller may not open.

**Ranking.** Each match scores 1 + `ts_rank` for a whole-word match (2 + `ts_rank` for a title)
and its strict word similarity (< 1) for a variant match; a meeting ranks by its best match, then
newest first (`coalesce(started_at, created_at)`), the meeting id breaking ties. Pages are keyset
pages: the cursor holds the folded query and the last row's rank, time and id.

**Answer.** Per meeting: id, title, platform, status, team, start and creation times, and up to
three `hits` in this order: the title, the best transcript segments, the minutes. A hit carries
`text` (Roman; for a segment its canonical text) with `marks`, the `[start, end)` offsets of the
matched words, trimmed to a window around the first mark; a transcript hit also carries the segment
id, speaker, start (seconds) and `text_raw` (the original script) with `raw_marks`. Snippets are
cut only for the meetings on the page.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import (
    ColumnElement,
    Float,
    Select,
    and_,
    case,
    func,
    literal,
    literal_column,
    or_,
    select,
    tuple_,
    union_all,
)
from sqlalchemy.orm import Session as Tx
from sqlalchemy.orm import aliased

from andai_core.db.models import Meeting, MomDocument, Team, TranscriptSegment
from andai_core.meetings.access import Actor, accessible
from andai_core.meetings.mom import Document
from andai_core.meetings.service import MeetingError
from andai_core.text import fold

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
MAX_QUERY_CHARS = 200
MIN_QUERY_CHARS = 2  # letters or digits after folding
MAX_CURSOR_CHARS = 512
MAX_HITS = 3
SNIPPET_CHARS = 240
SIMILAR = 0.5  # pg_trgm's default strict_word_similarity_threshold, which `<<%` uses

BAD_QUERY = MeetingError(
    422, "bad_query", f"a search needs 2 to {MAX_QUERY_CHARS} characters with letters or digits"
)
BAD_CURSOR = MeetingError(422, "bad_cursor", "this cursor is not valid for this search")

SIMPLE = literal_column("'simple'::regconfig")
SORT_TIME = func.coalesce(Meeting.started_at, Meeting.created_at)


# ─── the query text ───────────────────────────────────────────────────────────────────────────────


def fold_query(q: str) -> str:
    """The folded form of a user's query, else the 422 refusal."""
    if len(q) > MAX_QUERY_CHARS:
        raise BAD_QUERY
    folded = fold(q)
    if len(folded.replace(" ", "")) < MIN_QUERY_CHARS:
        raise BAD_QUERY
    return folded


# ─── cursor ───────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Position:
    """Where a page starts: after the meeting with this rank, sort time and id."""

    folded: str
    rank: float
    sort_time: datetime
    meeting_id: uuid.UUID


def encode_cursor(position: Position) -> str:
    raw = json.dumps(
        {
            "q": position.folded,
            "r": position.rank,
            "t": position.sort_time.isoformat(),
            "i": str(position.meeting_id),
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str, folded: str) -> Position:
    if not cursor or len(cursor) > MAX_CURSOR_CHARS:
        raise BAD_CURSOR
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        position = Position(
            folded=str(raw["q"]),
            rank=float(raw["r"]),
            sort_time=datetime.fromisoformat(raw["t"]),
            meeting_id=uuid.UUID(raw["i"]),
        )
    except (binascii.Error, UnicodeDecodeError, ValueError, KeyError, TypeError):
        raise BAD_CURSOR from None
    if position.folded != folded or position.sort_time.tzinfo is None:
        raise BAD_CURSOR
    return position


# ─── the SQL ──────────────────────────────────────────────────────────────────────────────────────


def _tsquery(folded: str) -> ColumnElement:
    return func.websearch_to_tsquery(SIMPLE, literal(folded))


@dataclass(frozen=True)
class _Text:
    """A searched text: its whole-word vector and, if it is searched for variants, its folded
    text."""

    vector: ColumnElement
    folded: ColumnElement | None


SEGMENT = _Text(TranscriptSegment.search_tsv, None)
TITLE = _Text(func.to_tsvector(SIMPLE, Meeting.title_search), Meeting.title_search)
MINUTES = _Text(MomDocument.search_tsv, MomDocument.search_text)


def _words(searched: _Text, folded: str) -> ColumnElement[bool]:
    """Every query word is a word of the text."""
    return searched.vector.op("@@")(_tsquery(folded))


def _matches(searched: _Text, folded: str) -> ColumnElement[bool]:
    if searched.folded is None:
        return _words(searched, folded)
    # The query is also a close spelling variant of whole words of the text.
    return or_(_words(searched, folded), literal(folded).op("<<%")(searched.folded))


def _rank(searched: _Text, folded: str, whole_word: float) -> ColumnElement[float]:
    exact = literal(whole_word) + func.ts_rank(searched.vector, _tsquery(folded))
    if searched.folded is None:
        return exact.cast(Float)
    return case(
        (_words(searched, folded), exact),
        else_=func.strict_word_similarity(literal(folded), searched.folded),
    ).cast(Float)


def _latest_minutes() -> ColumnElement[bool]:
    newer = aliased(MomDocument)
    return MomDocument.version == (
        select(func.max(newer.version))
        .where(newer.meeting_id == MomDocument.meeting_id)
        .scalar_subquery()
    )


def _hits(actor: Actor, folded: str):
    """(meeting_id, rank) of every readable match: segments, titles and latest minutes."""
    segments = (
        select(TranscriptSegment.meeting_id, _rank(SEGMENT, folded, 1.0))
        .join(
            Meeting,
            and_(
                Meeting.id == TranscriptSegment.meeting_id,
                Meeting.org_id == TranscriptSegment.org_id,
            ),
        )
        .where(
            TranscriptSegment.org_id == actor.org_id,
            _matches(SEGMENT, folded),
            accessible(actor),
        )
    )
    titles = select(Meeting.id, _rank(TITLE, folded, 2.0)).where(
        Meeting.title_search.is_not(None),
        _matches(TITLE, folded),
        accessible(actor),
    )
    minutes = (
        select(MomDocument.meeting_id, _rank(MINUTES, folded, 1.0))
        .join(
            Meeting,
            and_(Meeting.id == MomDocument.meeting_id, Meeting.org_id == MomDocument.org_id),
        )
        .where(
            MomDocument.org_id == actor.org_id,
            _latest_minutes(),
            _matches(MINUTES, folded),
            accessible(actor),
        )
    )
    every = union_all(segments, titles, minutes).subquery("hits")
    meeting_id, rank = every.c[0], every.c[1]
    return (
        select(meeting_id.label("meeting_id"), func.max(rank).label("rank"))
        .group_by(meeting_id)
        .cte("matched")
    )


def matches_query(actor: Actor, folded: str, *, limit: int, after: Position | None) -> Select:
    """One page of readable matching meetings, best first, with the total count of them."""
    matched = _hits(actor, folded)
    total = select(func.count()).select_from(matched).scalar_subquery()
    conditions = [Meeting.org_id == actor.org_id]
    if after is not None:
        # rank DESC, time DESC, id DESC: rows after `after` compare lower on the tuple.
        conditions.append(
            tuple_(matched.c.rank, SORT_TIME, Meeting.id)
            < tuple_(
                literal(after.rank, Float), literal(after.sort_time), literal(after.meeting_id)
            )
        )
    return (
        select(
            Meeting,
            Team.name.label("team_name"),
            matched.c.rank,
            SORT_TIME.label("sort_time"),
            total.label("total"),
            and_(Meeting.title_search.is_not(None), _matches(TITLE, folded)).label("title_hit"),
        )
        .join(matched, matched.c.meeting_id == Meeting.id)
        .outerjoin(Team, and_(Team.id == Meeting.team_id, Team.org_id == Meeting.org_id))
        .where(*conditions)
        .order_by(matched.c.rank.desc(), SORT_TIME.desc(), Meeting.id.desc())
        .limit(limit)
    )


def _segment_hits(tx: Tx, actor: Actor, folded: str, meeting_ids: list[uuid.UUID]) -> dict:
    """The best `MAX_HITS` matching segments of each page meeting (already access-checked)."""
    if not meeting_ids:
        return {}
    rank = _rank(SEGMENT, folded, 1.0)
    ranked = (
        select(
            TranscriptSegment,
            func.row_number()
            .over(
                partition_by=TranscriptSegment.meeting_id,
                order_by=(rank.desc(), TranscriptSegment.seq),
            )
            .label("n"),
        )
        .where(
            TranscriptSegment.org_id == actor.org_id,
            TranscriptSegment.meeting_id.in_(meeting_ids),
            _matches(SEGMENT, folded),
        )
        .subquery()
    )
    best = aliased(TranscriptSegment, ranked)
    rows = tx.execute(
        select(best).where(ranked.c.n <= MAX_HITS).order_by(ranked.c.meeting_id, ranked.c.n)
    ).scalars()
    found: dict[uuid.UUID, list[TranscriptSegment]] = {}
    for segment in rows:
        found.setdefault(segment.meeting_id, []).append(segment)
    return found


def _minutes_hits(tx: Tx, actor: Actor, folded: str, meeting_ids: list[uuid.UUID]) -> dict:
    if not meeting_ids:
        return {}
    rows = tx.execute(
        select(MomDocument.meeting_id, MomDocument.body).where(
            MomDocument.org_id == actor.org_id,
            MomDocument.meeting_id.in_(meeting_ids),
            _latest_minutes(),
            _matches(MINUTES, folded),
        )
    ).all()
    return {meeting_id: body for meeting_id, body in rows}


# ─── snippets ─────────────────────────────────────────────────────────────────────────────────────

# A word: letters, digits and the Devanagari block (its vowel signs are combining marks), without
# the danda and double danda.
_WORD = re.compile(r"[\wऀ-ॣ०-ॿ‌‍]+")


def _trigrams(word: str) -> set[str]:
    padded = f"  {word} "
    return {padded[i : i + 3] for i in range(len(padded) - 2)}


def _similar(word: str, term: str) -> bool:
    a, b = _trigrams(word), _trigrams(term)
    return len(a & b) / len(a | b) >= SIMILAR


def _hit(word: str, terms: list[str], variants: bool) -> bool:
    folded = fold(word).split()
    return any(w == t or (variants and _similar(w, t)) for w in folded for t in terms)


def snippet(
    text: str, terms: list[str], *, variants: bool = False
) -> tuple[str, list[list[int]]]:
    """`text` cut to about `SNIPPET_CHARS` around its first matched word, and the `[start, end)`
    offsets of the matched words in the cut text: words whose fold is a query word, and with
    `variants` also close spelling variants of one. A cut side is marked with `…`."""
    marks = [
        [m.start(), m.end()] for m in _WORD.finditer(text) if _hit(m.group(0), terms, variants)
    ]
    if len(text) <= SNIPPET_CHARS:
        return text, marks
    first = marks[0][0] if marks else 0
    start = max(0, first - SNIPPET_CHARS // 3)
    end = min(len(text), start + SNIPPET_CHARS)
    start = max(0, end - SNIPPET_CHARS)
    # Do not cut through a word.
    while 0 < start < len(text) and _WORD.match(text[start - 1]):
        start += 1
    while 0 < end < len(text) and _WORD.match(text[end]):
        end -= 1
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    shift = len(prefix) - start
    kept = [[a + shift, b + shift] for a, b in marks if a >= start and b <= end]
    return prefix + text[start:end] + suffix, kept


def _transcript_hit(segment: TranscriptSegment, terms: list[str]) -> dict:
    text_, marks = snippet(segment.text_canonical, terms)
    raw, raw_marks = snippet(segment.text_raw, terms)
    return {
        "source": "transcript",
        "segment_id": segment.id,
        "speaker": segment.speaker,
        "start": segment.start_s,
        "text": text_,
        "marks": marks,
        "text_raw": raw,
        "raw_marks": raw_marks,
    }


def _quoted(source: str, text_: str, terms: list[str]) -> dict:
    """A title or minutes hit; these texts are searched for spelling variants too."""
    cut, marks = snippet(text_, terms, variants=True)
    return {"source": source, "text": cut, "marks": marks}


def _minutes_hit(body: dict, terms: list[str]) -> dict | None:
    """The first piece of the minutes (summary, decisions, action items, topics, risks) holding a
    matched word."""
    pieces = Document.model_validate(body).texts()
    for piece in pieces:
        quoted = _quoted("minutes", piece, terms)
        if quoted["marks"]:
            return quoted
    return _quoted("minutes", pieces[0], terms) if pieces else None


# ─── the answer ───────────────────────────────────────────────────────────────────────────────────


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def search(
    tx: Tx, actor: Actor, q: str, *, limit: int = DEFAULT_LIMIT, cursor: str | None = None
) -> dict:
    folded = fold_query(q)
    after = None if cursor is None else decode_cursor(cursor, folded)
    rows = tx.execute(matches_query(actor, folded, limit=limit + 1, after=after)).all()
    total = rows[0].total if rows else (0 if after is None else _count(tx, actor, folded))
    more = len(rows) > limit
    rows = rows[:limit]
    ids = [row.Meeting.id for row in rows]
    segments = _segment_hits(tx, actor, folded, ids)
    minutes = _minutes_hits(tx, actor, folded, ids)
    terms = folded.split()
    results = []
    for row in rows:
        meeting = row.Meeting
        hits: list[dict] = []
        if row.title_hit and meeting.title:
            hits.append(_quoted("title", meeting.title, terms))
        hits += [_transcript_hit(s, terms) for s in segments.get(meeting.id, [])]
        if meeting.id in minutes and (found := _minutes_hit(minutes[meeting.id], terms)):
            hits.append(found)
        results.append(
            {
                "id": str(meeting.id),
                "title": meeting.title,
                "platform": meeting.platform,
                "status": meeting.status,
                "team": (
                    None
                    if meeting.team_id is None
                    else {"id": str(meeting.team_id), "name": row.team_name}
                ),
                "started_at": _iso(meeting.started_at),
                "created_at": _iso(meeting.created_at),
                "hits": hits[:MAX_HITS],
            }
        )
    next_cursor = None
    if more:
        last = rows[-1]
        next_cursor = encode_cursor(Position(folded, last.rank, last.sort_time, last.Meeting.id))
    return {"query": q.strip(), "total": total, "results": results, "next_cursor": next_cursor}


def _count(tx: Tx, actor: Actor, folded: str) -> int:
    """The total for a page past the last match (no row to carry it)."""
    matched = _hits(actor, folded)
    return tx.scalar(select(func.count()).select_from(matched)) or 0


__all__ = [
    "BAD_CURSOR",
    "BAD_QUERY",
    "DEFAULT_LIMIT",
    "MAX_CURSOR_CHARS",
    "MAX_LIMIT",
    "MAX_QUERY_CHARS",
    "SNIPPET_CHARS",
    "fold_query",
    "matches_query",
    "search",
    "snippet",
]
