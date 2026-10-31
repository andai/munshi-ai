"""`GET /api/v1/search` (PORT-05): keyword search over titles, transcripts and minutes, returning
only meetings the caller may read, with snippets; a Roman query finds Devanagari-origin speech.

The fixture org holds three meetings of two teams with Devanagari, Roman and mixed segments, a
title each and minutes on one; a second org holds a meeting with the same words. For every caller
and query the results are exactly the readable matching meetings, and the total counts those only.
"""

from __future__ import annotations

import math
import random
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, delete, insert, text

from andai_core.db.models import Meeting, MeetingShare
from andai_core.db.session import transaction
from andai_core.meetings import mom
from andai_core.search import service as search
from conftest import (
    AccessOrg,
    access_org,
    make_harness,
    mom_body,
    new_member,
    new_team,
    seed_meeting,
    seed_segments,
    sign_in,
)

PATH = "/api/v1/search"
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


@dataclass
class World:
    org: AccessOrg
    m1: uuid.UUID  # requester's, team, shared with user_grantee; minutes
    m2: uuid.UUID  # requester's, team, not shared
    m3: uuid.UUID  # owner's, shared_team, not shared
    other: uuid.UUID  # the other org's, same words, minutes
    segments: dict[uuid.UUID, list[str]]


@pytest.fixture(scope="module")
def world(app_engine: Engine) -> World:
    w = access_org(app_engine)
    people = w.people
    m1 = seed_meeting(
        app_engine, w.org_id, people["requester"].user_id, w.team,
        shares=(("user", people["user_grantee"].user_id),),
        title="Budget review", started_at=T0,
    )  # fmt: skip
    m2 = seed_meeting(
        app_engine, w.org_id, people["requester"].user_id, w.team,
        title="Weekly sync", started_at=T0 - timedelta(days=1),
    )  # fmt: skip
    m3 = seed_meeting(
        app_engine, w.org_id, people["owner"].user_id, w.shared_team,
        title="Hiring plan", started_at=T0 - timedelta(days=2),
    )  # fmt: skip
    other_owner = people["other_org"]
    other = seed_meeting(
        app_engine, other_owner.org_id, other_owner.user_id, None,
        title="Budget review", started_at=T0,
    )  # fmt: skip
    segments = {
        m1: seed_segments(
            app_engine, w.org_id, m1,
            [("Asha", "कल तक deadline है"), ("Ravi", "क्या हम budget final करें?")],
        ),
        m2: seed_segments(
            app_engine, w.org_id, m2, ["theek hai, kal baat karte hain", "kya plan hai"]
        ),
        m3: seed_segments(app_engine, w.org_id, m3, ["ठीक है, deadline शुक्रवार है"]),
        other: seed_segments(
            app_engine, other_owner.org_id, other, ["कल deadline क्या है, ठीक है? budget"]
        ),
    }  # fmt: skip
    with transaction(app_engine, org_id=w.org_id) as tx:
        mom.seed_version(
            tx, w.org_id, m1,
            mom_body(segments[m1], summary="Vendor contract sign karna hai"),
        )  # fmt: skip
    with transaction(app_engine, org_id=other_owner.org_id) as tx:
        mom.seed_version(
            tx, other_owner.org_id, other,
            mom_body(segments[other], summary="Vendor contract sign karna hai"),
        )  # fmt: skip
    return World(w, m1, m2, m3, other, segments)


def _search(client, q: str, **params) -> dict:
    response = client.get(PATH, params={"q": q, **params})
    assert response.status_code == 200, response.text
    return response.json()


def _ids(body: dict) -> set[str]:
    return {r["id"] for r in body["results"]}


# ─── exactly the readable matching meetings ───────────────────────────────────────────────────────

# Which fixture meetings each query matches, before access.
MATCHES = {
    "kal": {"m1", "m2", "other"},  # कल (Devanagari) in m1 and the other org, kal in m2
    "kya": {"m1", "m2", "other"},  # क्या / kya
    "theek hai": {"m2", "m3", "other"},  # theek hai / ठीक है
    "deadline": {"m1", "m3", "other"},
    "hiring": {"m3"},  # title only
    "contract": {"m1", "other"},  # minutes only
    "budget": {"m1", "other"},  # title, segment and minutes
}
READS = {
    "requester": {"m1", "m2"},
    "lead": {"m1", "m2"},
    "user_grantee": {"m1"},
    "team_grantee": set(),  # m3 is shared_team's, but led by nobody and shared with no one
    "unrelated": set(),
    "admin": {"m1", "m2", "m3"},
    "owner": {"m1", "m2", "m3"},
    "other_org": {"other"},
}


@pytest.mark.parametrize("q", sorted(MATCHES))
@pytest.mark.parametrize("caller", sorted(READS))
def test_results_are_exactly_the_readable_matching_meetings(
    world: World, caller: str, q: str
) -> None:
    names = {"m1": world.m1, "m2": world.m2, "m3": world.m3, "other": world.other}

    body = _search(world.org.client(caller), q)

    expected = {str(names[n]) for n in MATCHES[q] & READS[caller]}
    assert _ids(body) == expected, (caller, q)
    assert body["total"] == len(expected)
    assert body["next_cursor"] is None


def test_a_revoked_share_drops_out_on_the_next_call(world: World) -> None:
    w = world.org
    unrelated = w.client("unrelated")
    with transaction(w.engine, org_id=w.org_id) as tx:
        tx.add(
            MeetingShare(
                org_id=w.org_id, meeting_id=world.m2, grantee_type="user",
                grantee_id=w.people["unrelated"].user_id,
                granted_by=w.people["requester"].user_id,
            )
        )  # fmt: skip
    assert _ids(_search(unrelated, "kal")) == {str(world.m2)}

    with transaction(w.engine, org_id=w.org_id) as tx:
        tx.execute(
            delete(MeetingShare).where(
                MeetingShare.meeting_id == world.m2,
                MeetingShare.grantee_id == w.people["unrelated"].user_id,
            )
        )

    body = _search(unrelated, "kal")
    assert (body["results"], body["total"]) == ([], 0)


# ─── folding, snippets, the result shape ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("q", ["kya", "kyaa", "KYA", "क्या", "  Kyaa  "])
def test_spelling_and_script_variants_meet(world: World, q: str) -> None:
    body = _search(world.org.client("requester"), q)

    assert _ids(body) == {str(world.m1), str(world.m2)}


def _marked(text_: str, marks: list[list[int]]) -> list[str]:
    return [text_[a:b] for a, b in marks]


def test_a_roman_query_highlights_the_devanagari_segment(world: World) -> None:
    body = _search(world.org.client("requester"), "kal")

    [m1] = [r for r in body["results"] if r["id"] == str(world.m1)]
    [hit] = [h for h in m1["hits"] if h["source"] == "transcript"]
    assert hit["segment_id"] == world.segments[world.m1][0]
    assert (hit["speaker"], hit["start"]) == ("Asha", 0.0)
    assert hit["text_raw"] == "कल तक deadline है"
    assert _marked(hit["text_raw"], hit["raw_marks"]) == ["कल"]
    assert hit["text"] == "kal tak deadline hai"
    assert _marked(hit["text"], hit["marks"]) == ["kal"]


def test_a_result_carries_what_the_dashboard_shows(world: World) -> None:
    body = _search(world.org.client("requester"), "budget")

    assert body["query"] == "budget"
    [m1] = body["results"]
    assert set(m1) == {
        "id", "title", "platform", "status", "team", "started_at", "created_at", "hits",
    }  # fmt: skip
    assert m1["title"] == "Budget review"
    assert m1["team"]["id"] == str(world.org.team)
    assert m1["started_at"] == T0.isoformat()
    assert 1 <= len(m1["hits"]) <= 3
    sources = [h["source"] for h in m1["hits"]]
    assert sources[0] == "title" and "transcript" in sources
    for h in m1["hits"]:
        assert any("budget" in s.lower() for s in _marked(h["text"], h["marks"])), h


def test_titles_and_minutes_match_spelling_variants(world: World) -> None:
    body = _search(world.org.client("requester"), "budgets")  # not a word anywhere

    [m1] = body["results"]
    assert m1["id"] == str(world.m1)
    sources = [h["source"] for h in m1["hits"]]
    assert sources == ["title", "minutes"], "transcripts match whole folded words only"
    assert _marked(m1["hits"][0]["text"], m1["hits"][0]["marks"]) == ["Budget"]


def test_a_minutes_only_hit_quotes_the_minutes(world: World) -> None:
    body = _search(world.org.client("requester"), "contract")

    [m1] = body["results"]
    [hit] = m1["hits"]
    assert hit["source"] == "minutes"
    assert hit["text"] == "Vendor contract sign karna hai"
    assert _marked(hit["text"], hit["marks"]) == ["contract"]


def test_no_more_than_three_hits_per_meeting(world: World) -> None:
    w = world.org
    chatty = seed_meeting(
        w.engine, w.org_id, w.people["requester"].user_id, w.team, title="kal ki baat"
    )
    seed_segments(w.engine, w.org_id, chatty, [f"kal {n}" for n in range(6)])

    [result] = [
        r for r in _search(w.client("requester"), "kal")["results"] if r["id"] == str(chatty)
    ]
    assert len(result["hits"]) == 3
    assert [h["source"] for h in result["hits"]] == ["title", "transcript", "transcript"]


def test_a_long_segment_is_trimmed_around_the_hit() -> None:
    raw = " ".join(["filler"] * 80 + ["deadline"] + ["filler"] * 80)

    snippet, marks = search.snippet(raw, ["deadline"])

    assert len(snippet) <= search.SNIPPET_CHARS + 2
    assert snippet.startswith("…") and snippet.endswith("…")
    assert _marked(snippet, marks) == ["deadline"]


@pytest.mark.parametrize("q", ["", " ", "a", "क्", "ि", "!!", "?" * 5, "x" * 201])
def test_empty_short_and_long_queries_are_422(world: World, q: str) -> None:
    response = world.org.client("requester").get(PATH, params={"q": q})

    assert response.status_code == 422, response.text


def test_a_missing_query_is_422(world: World) -> None:
    assert world.org.client("requester").get(PATH).status_code == 422


def test_a_query_with_search_syntax_is_taken_as_words(world: World) -> None:
    for q in ('"kal" -tak', "kal & | ! :*", "kal) (tak"):
        response = world.org.client("requester").get(PATH, params={"q": q})
        assert response.status_code == 200, (q, response.text)


# ─── pages ────────────────────────────────────────────────────────────────────────────────────────


def test_pages_cover_every_match_once(app_engine: Engine) -> None:
    owner = new_member(app_engine)
    team = new_team(app_engine, owner.org_id, {owner.user_id: "lead"})
    made = []
    for n in range(23):
        meeting_id = seed_meeting(
            app_engine, owner.org_id, owner.user_id, team,
            started_at=T0 - timedelta(hours=n % 5),  # ties in time, broken by id
        )  # fmt: skip
        seed_segments(app_engine, owner.org_id, meeting_id, ["kal milte hain"] * (1 + n % 3))
        made.append(str(meeting_id))
    client = sign_in(make_harness(app_engine), owner)

    seen, cursor, totals = [], None, set()
    for _ in range(10):
        params = {"limit": 5, **({"cursor": cursor} if cursor else {})}
        body = _search(client, "kal", **params)
        seen += [r["id"] for r in body["results"]]
        totals.add(body["total"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(made) and len(seen) == len(set(seen))
    assert totals == {23}
    bad = client.get(PATH, params={"q": "deadline", "cursor": body_cursor(client)})
    assert bad.status_code == 422 and bad.json()["code"] == "bad_cursor"


def body_cursor(client) -> str:
    return _search(client, "kal", limit=1)["next_cursor"]


# ─── speed at 1,000 meetings × 200 segments ───────────────────────────────────────────────────────

FILLER = ["hai", "to", "ki", "ka", "ke", "aur", "matlab", "haan", "nahin", "ok", "so", "the", "we"]


def _segment_text(rng: random.Random, vocabulary: list[str]) -> str:
    words = []
    for _ in range(rng.randint(8, 18)):
        roll = rng.random()
        if roll < 0.004:
            words.append("kal")
        elif roll < 0.0045:
            words.append("deadline")
        elif roll < 0.5:
            words.append(rng.choice(FILLER))
        else:
            words.append(rng.choice(vocabulary))
    return " ".join(words)


def _p95(samples: list[float]) -> float:
    return sorted(samples)[math.ceil(0.95 * len(samples)) - 1]


@pytest.fixture(scope="module")
def big(postgres, migrated):
    """A fresh database with one org: 1,000 past meetings × 200 segments (200,000), 3 teams."""
    # A module-scoped fresh database (conftest's `fresh_app_engine` is function-scoped).
    from alembic import command
    from sqlalchemy import create_engine

    from andai_core.db.session import create_app_engine
    from conftest import alembic_config

    name = f"andai_{uuid.uuid4().hex[:12]}"
    admin = create_engine(postgres.url("postgres", postgres.superuser_password, "postgres"))
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"CREATE DATABASE {name} OWNER andai_owner"))
        conn.execute(text(f"REVOKE ALL ON DATABASE {name} FROM PUBLIC"))
        conn.execute(text(f"GRANT CONNECT ON DATABASE {name} TO andai_owner, andai_app"))
    db_admin = create_engine(postgres.url("postgres", postgres.superuser_password, name))
    with db_admin.begin() as conn:
        conn.execute(text("REVOKE ALL ON SCHEMA public FROM PUBLIC"))
        conn.execute(text("GRANT USAGE ON SCHEMA public TO andai_app, andai_scan"))
    command.upgrade(
        alembic_config(postgres.url("andai_owner", postgres.owner_password, name)), "head"
    )
    engine = create_app_engine(postgres.url("andai_app", postgres.app_password, name))
    try:
        yield _seed_big(engine, db_admin)
    finally:
        engine.dispose()
        db_admin.dispose()
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        admin.dispose()


@dataclass
class Big:
    engine: Engine
    org: AccessOrg
    superuser: Engine  # the same database, past row-level security


def _seed_big(engine: Engine, db_admin: Engine) -> Big:
    from andai_core.db.models import TranscriptSegment
    from andai_core.text import fold

    w = access_org(engine)
    teams = [w.team, w.shared_team, new_team(engine, w.org_id, {})]
    requesters = [w.people[c].user_id for c in ("requester", "lead", "team_member", "owner")]
    rng = random.Random(5)
    vocabulary = [f"w{n:04d}x" for n in range(3000)]
    meetings, segments = [], []
    for n in range(1000):
        meeting_id = uuid.uuid4()
        title = "Budget review" if n % 50 == 0 else f"Meeting {n}"
        meetings.append(
            {
                "id": meeting_id,
                "org_id": w.org_id,
                "team_id": teams[n % 3],
                "requested_by": requesters[n % len(requesters)],
                "platform": "zoom",
                "join_url": f"https://zoom.us/j/{n}",
                "join_url_key": f"zoom.us/j/{meeting_id.hex}",
                "status": "done",
                "started_at": T0 - timedelta(hours=n),
                "title": title,
                "title_search": fold(title),
                "transcript_state": "stored",
            }
        )
        for seq in range(200):
            line = _segment_text(rng, vocabulary)
            segments.append(
                {
                    "meeting_id": meeting_id,
                    "id": f"seg_{n}_{seq}",
                    "org_id": w.org_id,
                    "seq": seq,
                    "start_s": seq * 5.0,
                    "speaker": "Asha",
                    "text_raw": line,
                    "text_canonical": line,
                    "script": "latn",
                    "search_text": fold(line),
                }
            )
    with transaction(engine, org_id=w.org_id) as tx:
        tx.execute(insert(Meeting.__table__), meetings)
    for start in range(0, len(segments), 20000):
        with transaction(engine, org_id=w.org_id) as tx:
            tx.execute(insert(TranscriptSegment.__table__), segments[start : start + 20000])
    with db_admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("VACUUM ANALYZE"))
    return Big(engine, w, db_admin)


SPEED_QUERIES = ("kal", "deadline", "budget", "kaal deadline", "hai")  # hai: ~40% of segments
SPEED_CALLERS = ("admin", "requester", "unrelated")


def test_search_is_fast_at_a_thousand_meetings(big: Big) -> None:
    timings: dict[tuple[str, str], list[float]] = {}
    for caller in SPEED_CALLERS:
        client = big.org.client(caller)
        for q in SPEED_QUERIES:
            _search(client, q)  # warm
            samples = []
            for _ in range(20):
                started = time.perf_counter()
                body = _search(client, q)
                samples.append(time.perf_counter() - started)
            timings[(caller, q)] = samples
            if caller == "unrelated":
                assert body["total"] == 0
            else:
                assert body["total"] > 0, (caller, q)
    report = {
        k: (round(_p95(v) * 1000, 1), round(sorted(v)[len(v) // 2] * 1000, 1))
        for k, v in timings.items()
    }
    print("search p95/median ms:", report)
    for key, samples in timings.items():
        assert _p95(samples) <= 0.5, (key, report[key])


def _explain(big: Big, caller: str, q: str) -> str:
    actor = big.org.actor(caller)
    with transaction(big.engine, org_id=actor.org_id, user_id=actor.user_id) as tx:
        compiled = search.matches_query(
            actor, search.fold_query(q), limit=21, after=None
        ).compile(dialect=tx.bind.dialect)
        rows = tx.connection().exec_driver_sql(
            f"EXPLAIN (ANALYZE, FORMAT TEXT) {compiled.string}", compiled.params
        ).scalars()
        return "\n".join(rows)


@pytest.mark.parametrize("caller", ["admin", "requester", "unrelated"])
def test_search_reads_the_stored_vectors(big: Big, caller: str) -> None:
    """Under FORCE ROW LEVEL SECURITY `@@` (not LEAKPROOF) is checked after each row's policy, so
    the app role's plan scans the org's segments; it must test their stored vector, never parse
    segment text and never run a trigram test on them."""
    plan = _explain(big, caller, "deadline")
    print(f"\n===== {caller} deadline\n{plan}")

    segment_filters = [
        line
        for above, line in zip(plan.splitlines(), plan.splitlines()[1:], strict=False)
        if "on transcript_segments" in above and "Filter:" in line
    ]
    assert segment_filters, plan
    for line in segment_filters:
        assert "search_tsv @@" in line, line
        assert "to_tsvector" not in line and "<<%" not in line, line


def test_the_segment_index_serves_a_reader_outside_row_level_security(big: Big) -> None:
    with big.superuser.connect() as conn:
        plan = "\n".join(
            conn.execute(
                text(
                    "EXPLAIN SELECT meeting_id FROM transcript_segments"
                    " WHERE search_tsv @@ websearch_to_tsquery('simple'::regconfig, 'deadline')"
                )
            ).scalars()
        )
    assert "ix_transcript_segments_search_tsv" in plan, plan
