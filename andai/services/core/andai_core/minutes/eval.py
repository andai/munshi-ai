"""The minutes gold set: labelled synthetic meetings run through the production path and scored
mechanically (MOM-02, D-45).

A fixture (`tests/mom_gold/{en,hi,hinglish}/NN-<slug>.json`, schema in that directory's README) is a
scripted meeting: attendees, a local meeting date, segments with short local ids (`s001`) and the
labels — the decisions and action items it plants (with the segments that support them, the owner
as the owner rule D-49 should decide it and the due date as D-51 resolves the phrase), distractors
the minutes must not pick up, and for long meetings one needle fact said only once at minute 45.

* `load` / `load_all` read fixtures; `validate` lists everything wrong with one (offline tests run
  it over the whole set); `count` gives the per-language totals.
* `run_fixture` runs one fixture exactly as the minutes job does — `minutes.generate` over the
  segments (`text_raw` as spoken, production-shaped segment ids), then the writer's generated-mode
  rules through `mom_writer.build_document` — and scores the stored document. No database.
  Like the minutes job, it retries a meeting whose generation fails with a transient
  `llm_unavailable` — the route unreachable or timed out (no status), or status 408, 429 or 5xx —
  as a new attempt after a doubling, capped wait. The schedule is a deliberate scaling of the
  production policy (8 attempts, 30 s doubling up to 30 min) so that a 40-meeting run stays
  bounded: `RETRY_ATTEMPTS` 4 attempts with waits of 10, 20 and 40 s (`RETRY_BASE_S`, capped at
  `RETRY_CAP_S`). An outage longer than about 70 s therefore still fails the meeting. Other 4xx
  statuses, off-schema and truncated replies end the meeting at once: the schema stop must see
  them. Every retry is recorded on the meeting and in the report's `retries`.
* `score` compares a stored document with the labels: a label matches an item of its section when
  every one of its `match` groups has an alternative in the item's text (fold-normalised, so
  Devanagari and Roman spellings meet; words of 6+ letters fuzzy at a 0.8 ratio, shorter ones
  as a prefix, numbers exact). A matched item is *supported* when it cites a labelled segment or
  one within ±2 segments of it. When several items match a label, the one answering it is taken:
  supported first, then with the labelled owner, then the labelled due date. Owners and due dates
  are compared exactly.
* `report` aggregates the runs: retries, recall on labelled decisions and action items, the stored-evidence
  validity, labelled support of matched items, owner and date accuracy, needles, truncation, the
  pre-filter invalid-id rate and dropped items per section, distractor hits (informational),
  schema failures, what the model wrote per meeting, and per-meeting latency with n / median /
  max (p95 only where n ≥ 20) per exclusive length band (≤ 30, 31–60, 61–90 min). Its `sc3`
  block states each criterion as pass or fail.
* `write_report` saves the report as JSON and Markdown. Neither names the route's host or key.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import statistics
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

from andai_core.llm.client import (
    ChatJson,
    ChatResult,
    LlmError,
    LlmInvalidOutput,
    LlmTruncated,
    LlmUnavailable,
)
from andai_core.meetings.mom import Document
from andai_core.meetings.mom_writer import build_document
from andai_core.meetings.transcript import segment_id
from andai_core.minutes import dates, templates
from andai_core.minutes.chunking import Segment, pack, refs, refs_in
from andai_core.minutes.generate import PART_TOKENS, Draft, budget, generate
from andai_core.minutes.owners import UNASSIGNED
from andai_core.text import fold

LANGUAGES = ("en", "hi", "hinglish")
LOCAL_ID = re.compile(r"^s\d{3,4}$")
SUPPORT_WINDOW = 2
FUZZY = 0.8
FUZZY_MIN_LEN = 6
NEEDLE_MINUTE = 45
BANDS = (("<=30", 0, 30), ("31-60", 31, 60), ("61-90", 61, 90))
P95_MIN_N = 20
RECALL_THRESHOLD = 0.8
SCHEMA_FAILURE_LIMIT = 0.1
DISTRACTOR_KINDS = ("not_agreed", "other_date")
# Retry of transient route errors: attempts in all, and the doubling wait's start and cap (s).
RETRY_ATTEMPTS = 4
RETRY_BASE_S = 10.0
RETRY_CAP_S = 60.0
# The fixed profile id gold runs store (no route host goes into it).
GOLD_PROFILE = uuid.UUID("00000000-0000-4000-8000-00000000901d")


# ─── fixtures ─────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Fixture:
    path: Path
    data: dict

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def language(self) -> str:
        return self.data["language"]

    @property
    def kind(self) -> str:
        return self.data["kind"]

    @property
    def duration_min(self) -> int:
        return int(self.data["duration_min"])

    @property
    def meeting_date(self) -> date:
        return date.fromisoformat(self.data["meeting_date"])

    @property
    def attendees(self) -> list[str]:
        return list(self.data["attendees"])

    @property
    def segments(self) -> list[dict]:
        return list(self.data["segments"])

    @property
    def labels(self) -> dict:
        return self.data["labels"]

    def labelled(self) -> list[tuple[str, dict]]:
        """(section, label) of every labelled decision and action item."""
        return [("decisions", d) for d in self.labels.get("decisions", [])] + [
            ("action_items", a) for a in self.labels.get("action_items", [])
        ]

    def needle(self) -> tuple[str, dict] | None:
        found = [(s, label) for s, label in self.labelled() if label.get("needle")]
        return found[0] if found else None

    def global_id(self, local: str) -> str:
        """The production-shaped segment id of a local id (stable per fixture)."""
        return segment_id(uuid.uuid5(uuid.NAMESPACE_URL, f"munshi-gold:{self.id}"), local)

    def index(self) -> dict[str, int]:
        """Production segment id → position in the transcript."""
        return {self.global_id(s["id"]): i for i, s in enumerate(self.segments)}

    def local_index(self) -> dict[str, int]:
        return {s["id"]: i for i, s in enumerate(self.segments)}

    def transcript(self) -> list[Segment]:
        return [
            Segment(
                id=self.global_id(s["id"]),
                start_s=float(s["start_s"]),
                speaker=s["speaker"],
                text=s["text"],
            )
            for s in self.segments
        ]


def dumps(data: dict) -> str:
    """A fixture's canonical file text: one segment and one label per line."""

    def one(value: object) -> str:
        return json.dumps(value, ensure_ascii=False)

    parts = []
    for key, value in data.items():
        if key == "segments":
            body = ",\n".join(f"  {one(s)}" for s in value)
            parts.append(f' "segments": [\n{body}\n ]')
        elif key == "labels":
            sections = []
            for name, items in value.items():
                body = ",\n".join(f"   {one(i)}" for i in items)
                sections.append(f"  {one(name)}: [\n{body}\n  ]" if items else f"  {one(name)}: []")
            parts.append(' "labels": {\n' + ",\n".join(sections) + "\n }")
        else:
            parts.append(f" {one(key)}: {one(value)}")
    return "{\n" + ",\n".join(parts) + "\n}\n"


def load(path: Path) -> Fixture:
    return Fixture(path, json.loads(path.read_text(encoding="utf-8")))


def load_all(root: Path) -> list[Fixture]:
    """Every fixture under `root`, by language then file name."""
    return [load(p) for lang in LANGUAGES for p in sorted((root / lang).glob("*.json"))]


# ─── matching ─────────────────────────────────────────────────────────────────────────────────────


def _token_hit(want: str, tokens: Sequence[str]) -> bool:
    if want in tokens:
        return True
    if any(c.isdigit() for c in want) or len(want) < 4:
        return False
    if len(want) < FUZZY_MIN_LEN:  # short words: a prefix only ("state" never meets "sakte")
        return any(t.startswith(want) for t in tokens)
    return any(
        len(t) >= 3 and (t.startswith(want) or SequenceMatcher(None, want, t).ratio() >= FUZZY)
        for t in tokens
    )


def _alt_hit(alt: str, tokens: Sequence[str]) -> bool:
    wanted = fold(alt).split()
    return bool(wanted) and all(_token_hit(w, tokens) for w in wanted)


def matches(groups: Sequence[Sequence[str]], text: str) -> bool:
    """Every group has an alternative whose (folded) words all occur in `text`."""
    tokens = fold(text).split()
    return bool(groups) and all(any(_alt_hit(a, tokens) for a in group) for group in groups)


# ─── validation ───────────────────────────────────────────────────────────────────────────────────


def _window_text(fixture: Fixture, local_ids: Iterable[str]) -> str:
    where = fixture.local_index()
    segs = fixture.segments
    picked: set[int] = set()
    for local in local_ids:
        if local in where:
            i = where[local]
            picked.update(range(max(0, i - SUPPORT_WINDOW), min(len(segs), i + SUPPORT_WINDOW + 1)))
    return " ".join(segs[i]["text"] for i in sorted(picked))


def validate(fixture: Fixture) -> list[str]:
    """Everything wrong with one fixture (empty when it is sound)."""
    d = fixture.data
    out: list[str] = []

    def bad(message: str) -> None:
        out.append(f"{fixture.path.name}: {message}")

    for key in ("id", "language", "kind", "title", "meeting_date", "duration_min", "attendees"):
        if key not in d:
            bad(f"missing {key!r}")
    if out:
        return out
    if fixture.path.stem != d["id"]:
        bad("id is not the file name")
    if d["language"] not in LANGUAGES or fixture.path.parent.name != d["language"]:
        bad("language does not match its directory")
    if not re.match(r"^\d{2}-[a-z0-9-]+$", d["id"]):
        bad("id is not NN-<slug>")
    try:
        meeting_date = fixture.meeting_date
    except ValueError:
        return [*out, f"{fixture.path.name}: meeting_date is not YYYY-MM-DD"]
    attendees = fixture.attendees
    if not attendees or len(set(attendees)) != len(attendees):
        bad("attendees empty or repeated")
    segs = fixture.segments
    ids = [s.get("id") for s in segs]
    if len(set(ids)) != len(ids) or not all(isinstance(i, str) and LOCAL_ID.match(i) for i in ids):
        bad("segment ids missing, repeated or not s<digits>")
    starts = [s.get("start_s") for s in segs]
    if not all(isinstance(t, int | float) for t in starts) or starts != sorted(starts):
        bad("start_s missing or not in order")
    for s in segs:
        if s.get("speaker") not in attendees:
            bad(f"{s.get('id')}: speaker {s.get('speaker')!r} is not an attendee")
        if not str(s.get("text") or "").strip():
            bad(f"{s.get('id')}: empty text")
    texts = [" ".join(str(s.get("text") or "").split()) for s in segs]
    if len(set(texts)) != len(texts):
        bad("a segment text is repeated")
    duration = fixture.duration_min
    if starts and isinstance(starts[-1], int | float):
        if not (duration - 3) * 60 <= starts[-1] <= duration * 60:
            bad(f"last segment at {starts[-1]} s does not fit duration_min {duration}")
    if d["kind"] == "short":
        if not 5 <= duration <= 15:
            bad("a short meeting is 5-15 min")
        if not 40 <= len(segs) <= 120:
            bad(f"a short meeting has 40-120 segments, not {len(segs)}")
    elif d["kind"] == "long":
        if duration not in (60, 90):
            bad("a long meeting is 60 or 90 min")
        if duration == 90 and len(segs) < 900:
            bad("a 90 min meeting has at least 900 segments")
    else:
        bad("kind is short or long")

    labels = d.get("labels") or {}
    decisions = labels.get("decisions") or []
    actions = labels.get("action_items") or []
    distractors = labels.get("distractors") or []
    label_ids = [x.get("id") for x in [*decisions, *actions, *distractors]]
    if len(set(label_ids)) != len(label_ids):
        bad("label ids repeated")
    where = fixture.local_index()
    for kind, label in [
        *(("decision", x) for x in decisions),
        *(("action", x) for x in actions),
        *(("distractor", x) for x in distractors),
    ]:
        name = label.get("id", "?")
        evidence = label.get("evidence") or []
        if not evidence or any(e not in where for e in evidence):
            bad(f"{name}: evidence empty or not segment ids")
        groups = label.get("match") or []
        if not groups or not all(
            isinstance(g, list) and g and all(a.strip() for a in g) for g in groups
        ):
            bad(f"{name}: match must be non-empty groups of alternatives")
        elif not matches(groups, _window_text(fixture, evidence)):
            bad(f"{name}: match groups do not occur in its evidence (±{SUPPORT_WINDOW})")
        if not str(label.get("fact") or "").strip():
            bad(f"{name}: no fact")
        if kind == "action":
            owner = label.get("owner")
            if owner != UNASSIGNED and owner not in attendees:
                bad(f"{name}: owner {owner!r} is neither an attendee nor unassigned")
            due, phrase = label.get("due"), label.get("due_phrase")
            if (due is None) != (phrase is None):
                bad(f"{name}: due and due_phrase go together")
            elif due is not None:
                try:
                    expected = date.fromisoformat(due)
                except ValueError:
                    bad(f"{name}: due is not YYYY-MM-DD")
                else:
                    resolved = dates.resolve(phrase, meeting_date)
                    if resolved != expected:
                        bad(
                            f"{name}: {phrase!r} on {meeting_date} resolves to {resolved},"
                            f" not {due}"
                        )
                    if phrase and not matches([[phrase]], _window_text(fixture, evidence)):
                        bad(f"{name}: due_phrase not said in its evidence (±{SUPPORT_WINDOW})")
        if kind == "distractor" and label.get("kind") not in DISTRACTOR_KINDS:
            bad(f"{name}: distractor kind is one of {DISTRACTOR_KINDS}")
        if kind != "distractor" and label.get("needle") and d["kind"] != "long":
            bad(f"{name}: only long meetings have a needle")

    needles = [x for x in [*decisions, *actions] if x.get("needle")]
    if d["kind"] == "short":
        if not 3 <= len(decisions) + len(actions) <= 6:
            bad("a short meeting has 3-6 labelled decisions and action items")
        if not decisions or not actions:
            bad("at least one decision and one action item")
        if not any(a.get("owner") == UNASSIGNED for a in actions):
            bad("at least one action item without a named owner")
        if not any(a.get("due") for a in actions):
            bad("at least one action item with a due date")
        for kind in DISTRACTOR_KINDS:
            if not any(x.get("kind") == kind for x in distractors):
                bad(f"at least one {kind} distractor")
    if d["kind"] == "long":
        if len(needles) != 1:
            bad("a long meeting has exactly one needle label")
        else:
            needle = needles[0]
            at = [segs[where[e]]["start_s"] for e in needle.get("evidence", []) if e in where]
            if not at or not all(
                (NEEDLE_MINUTE - 1) * 60 <= t < (NEEDLE_MINUTE + 1) * 60 for t in at
            ):
                bad("the needle's evidence is not at minute 45")
            said = [
                s["id"]
                for s in segs
                if s["id"] not in needle.get("evidence", [])
                and matches(needle.get("match") or [], s["text"])
            ]
            if said:
                bad(f"the needle fact is also said in {said[:3]}")
        if len(decisions) + len(actions) < 4:
            bad("a long meeting has at least 4 labelled decisions and action items")
    return out


def count(fixtures: Sequence[Fixture]) -> dict:
    per = {lang: sum(1 for f in fixtures if f.language == lang) for lang in LANGUAGES}
    long = [f for f in fixtures if f.kind == "long"]
    return {
        **per,
        "total": len(fixtures),
        "long": len(long),
        "long_90": sum(1 for f in long if f.duration_min == 90),
        "needles": sum(1 for f in long if f.needle() is not None),
        "labels": sum(len(f.labelled()) for f in fixtures),
    }


# ─── scoring ──────────────────────────────────────────────────────────────────────────────────────

SECTION_FIELDS = {"decisions": "decisions", "action_items": "actionItems"}


@dataclass
class LabelResult:
    label: str
    section: str
    found: bool
    supported: bool | None = None
    owner_expected: str | None = None
    owner_got: str | None = None
    due_expected: str | None = None
    due_got: str | None = None
    needle: bool = False

    @property
    def owner_ok(self) -> bool | None:
        return (
            None
            if not self.found or self.section != "action_items"
            else (self.owner_expected == self.owner_got)
        )

    @property
    def due_ok(self) -> bool | None:
        return (
            None
            if not self.found or self.section != "action_items"
            else (self.due_expected == self.due_got)
        )


@dataclass
class Score:
    labels: list[LabelResult] = field(default_factory=list)
    stored: dict[str, int] = field(default_factory=dict)  # items per section
    stored_invalid: int = 0  # stored items citing an id that is not a segment (must be 0)
    matched_items: int = 0  # stored decisions/action items matching a label
    matched_supported: int = 0
    distractor_hits: list[str] = field(default_factory=list)


def _support(evidence: Iterable[str], label: dict, fixture: Fixture, index: dict[str, int]) -> bool:
    where = fixture.local_index()
    wanted = [where[e] for e in label.get("evidence", []) if e in where]
    cited = [index[e] for e in evidence if e in index]
    return any(abs(c - w) <= SUPPORT_WINDOW for c in cited for w in wanted)


def _text(item) -> str:
    return getattr(item, "text", None) or getattr(item, "title", "")


def _rank(item, label: dict, fixture: Fixture, index: dict[str, int]) -> tuple[bool, bool, bool]:
    """Of several items matching a label, the one that answers it: supported first, then with the
    labelled owner, then with the labelled due date (`max` keeps the earliest of equals)."""
    due = getattr(item, "due", None)
    return (
        _support(item.evidence, label, fixture, index),
        getattr(item, "owner", None) == label.get("owner"),
        (due.isoformat() if due is not None else None) == label.get("due"),
    )


def score(fixture: Fixture, doc: Document) -> Score:
    """The stored document `doc` against the fixture's labels."""
    index = fixture.index()
    result = Score(
        stored={
            "decisions": len(doc.decisions),
            "action_items": len(doc.action_items),
            "topics": len(doc.topics),
            "risks": len(doc.risks),
        }
    )
    for items in (doc.decisions, doc.action_items, doc.topics, doc.risks):
        result.stored_invalid += sum(1 for i in items if any(e not in index for e in i.evidence))
    sections = {"decisions": doc.decisions, "action_items": doc.action_items}
    for section, label in fixture.labelled():
        pool = list(sections[section])
        if label.get("needle"):  # the needle may surface as either kind of item
            pool = list(doc.decisions) + list(doc.action_items)
        hits = [i for i in pool if matches(label["match"], _text(i))]
        supported = [i for i in hits if _support(i.evidence, label, fixture, index)]
        best = max(hits, key=lambda i: _rank(i, label, fixture, index), default=None)
        lr = LabelResult(
            label=label["id"],
            section=section,
            found=best is not None,
            needle=bool(label.get("needle")),
        )
        if best is not None:
            lr.supported = bool(supported)
            if section == "action_items":
                lr.owner_expected = label["owner"]
                lr.due_expected = label.get("due")
                lr.owner_got = getattr(best, "owner", None)
                due = getattr(best, "due", None)
                lr.due_got = due.isoformat() if due is not None else None
        result.labels.append(lr)
    for section, items in sections.items():
        labels = [label for s, label in fixture.labelled() if s == section or label.get("needle")]
        for item in items:
            mine = [label for label in labels if matches(label["match"], _text(item))]
            if mine:
                result.matched_items += 1
                if any(_support(item.evidence, label, fixture, index) for label in mine):
                    result.matched_supported += 1
    for distractor in fixture.labels.get("distractors", []):
        if distractor.get("kind") != "not_agreed":
            continue
        for item in [*doc.decisions, *doc.action_items]:
            if matches(distractor["match"], _text(item)) and _support(
                item.evidence, distractor, fixture, index
            ):
                result.distractor_hits.append(distractor["id"])
                break
    return result


# ─── one run ──────────────────────────────────────────────────────────────────────────────────────


@dataclass
class CallStats:
    calls: int = 0  # logical calls (one per map, single or reduce prompt)
    repaired: int = 0  # calls whose first reply was off-schema and a repair retry was needed
    schema_failures: int = 0  # calls that ended off-schema after the repair
    truncated: int = 0
    unavailable: int = 0
    retried_calls: int = 0  # calls made in attempts that failed and were retried (in `calls` too)


class Counting:
    """Wraps a `chat_json` and counts what happened to every call."""

    def __init__(self, inner: ChatJson, stats: CallStats) -> None:
        self.inner = inner
        self.stats = stats

    async def __call__(self, messages: list[dict], schema, *, name: str) -> ChatResult:
        self.stats.calls += 1
        try:
            result = await self.inner(messages, schema, name=name)
        except LlmInvalidOutput:
            self.stats.schema_failures += 1
            raise
        except LlmTruncated:
            self.stats.truncated += 1
            raise
        except LlmError:
            self.stats.unavailable += 1
            raise
        if result.usage.calls > 1:
            self.stats.repaired += 1
        return result


@dataclass
class MeetingRun:
    fixture: str
    language: str
    duration_min: int
    segments: int
    labels_total: int = 0  # labelled decisions and action items of the fixture
    seconds: float = 0.0
    parts: int = 0
    reduce_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    coverage_ok: bool = True  # every segment id was in some prompt part
    error: str | None = None
    attempts: int = 1  # generation attempts (more than 1 after a transient route error)
    retry_errors: list[str] = field(default_factory=list)  # each retried attempt's error
    retry_wait_s: float = 0.0  # time waited between attempts
    write: dict | None = None  # the writer's report (invalid ids, dropped items per section)
    cited_ids: int = 0  # ids the draft cited before the writer's filter
    draft: dict | None = None  # what the model wrote (decisions, action items), ids made local
    score: Score | None = None


def _as_written(draft: Draft, fixture: Fixture) -> dict:
    """The draft's decisions and action items as the model wrote them, segment ids shown as the
    fixture's local ids (`?<id>` for an id that is not a segment), for reading a run's misses."""
    local = {fixture.global_id(s["id"]): s["id"] for s in fixture.segments}

    def ids(evidence: Sequence[str]) -> list[str]:
        return [local.get(e, f"?{e}") for e in evidence]

    return {
        "summary": draft.summary,
        "decisions": [{"text": d.text, "evidence": ids(d.evidence)} for d in draft.decisions],
        "action_items": [
            {
                "text": a.text,
                "owner": a.owner,
                "owner_confidence": a.owner_confidence,
                "due_text": a.due_text,
                "due": a.due,
                "evidence": ids(a.evidence),
            }
            for a in draft.action_items
        ],
    }


def transient(exc: LlmError) -> bool:
    """A route error worth another attempt: unreachable or timed out, or status 408, 429 or 5xx."""
    if not isinstance(exc, LlmUnavailable):
        return False
    return exc.status is None or exc.status in (408, 429) or exc.status >= 500


def retry_wait(attempt: int) -> float:
    """The wait after the `attempt`-th failed attempt (1-based)."""
    return min(RETRY_BASE_S * 2 ** max(attempt - 1, 0), RETRY_CAP_S)


def _error_name(exc: LlmError) -> str:
    status = getattr(exc, "status", None)
    return exc.code if status is None else f"{exc.code}:{status}"


def _cited(draft: Draft) -> int:
    return sum(
        len(item.evidence)
        for section in (draft.decisions, draft.action_items, draft.topics, draft.risks)
        for item in section
    )


async def run_fixture(
    llm: ChatJson,
    fixture: Fixture,
    *,
    model: str,
    context_tokens: int,
    max_output_tokens: int,
    part_tokens: int = PART_TOKENS,
    template_id: str = templates.DEFAULT_TEMPLATE,
    clock: Callable[[], float] = time.monotonic,
    attempts: int = RETRY_ATTEMPTS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    stats: CallStats | None = None,
) -> MeetingRun:
    """Generate, apply the writer's rules and score one fixture (never raises for the model).
    A transient route error is retried up to `attempts` in all; `stats` (the one the `Counting`
    wrapper of `llm` updates) gets the calls of retried attempts in `retried_calls`."""
    template = templates.get(template_id)
    language = fixture.language
    segments = fixture.transcript()
    run = MeetingRun(
        fixture.id, language, fixture.duration_min, len(segments), len(fixture.labelled())
    )
    room = budget(
        template, language, fixture.attendees, fixture.meeting_date,  # type: ignore[arg-type]
        context_tokens=context_tokens, max_output_tokens=max_output_tokens,
        part_tokens=part_tokens,
    )  # fmt: skip
    packed = {n for part in pack(segments, room) for n in refs_in(part)}
    run.coverage_ok = packed == set(refs(segments))
    while True:
        started = clock()
        calls_before = stats.calls if stats is not None else 0
        try:
            generation = await generate(
                llm, segments, fixture.attendees, template, language,  # type: ignore[arg-type]
                fixture.meeting_date, context_tokens=context_tokens,
                max_output_tokens=max_output_tokens, part_tokens=part_tokens,
            )  # fmt: skip
        except LlmError as exc:
            run.seconds = clock() - started
            if not transient(exc) or run.attempts >= attempts:
                run.error = exc.code
                return run
            if stats is not None:
                stats.retried_calls += stats.calls - calls_before
            run.retry_errors.append(_error_name(exc))
            wait = retry_wait(run.attempts)
            run.retry_wait_s += wait
            run.attempts += 1
            await sleep(wait)
            continue
        break
    doc, report = build_document(
        generation.draft,
        segment_ids=[s.id for s in segments],
        attendees=[{"name": a, "source": "invite"} for a in fixture.attendees],
        meeting_date=fixture.meeting_date,
        template_id=template_id,
        output_language=language,
        model=model,
        llm_profile_id=GOLD_PROFILE,
    )
    run.seconds = clock() - started
    run.parts = generation.parts
    run.reduce_calls = generation.reduce_calls
    run.prompt_tokens = generation.usage.prompt_tokens
    run.completion_tokens = generation.usage.completion_tokens
    run.write = report.as_dict()
    run.cited_ids = _cited(generation.draft)
    run.draft = _as_written(generation.draft, fixture)
    run.score = score(fixture, doc)
    return run


# ─── the report ───────────────────────────────────────────────────────────────────────────────────


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 4)


def _p95(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def bands(runs: Sequence[MeetingRun]) -> dict:
    out = {}
    for name, low, high in BANDS:
        values = [r.seconds for r in runs if r.error is None and low <= r.duration_min <= high]
        band: dict = {"n": len(values)}
        if values:
            band["median_s"] = round(statistics.median(values), 2)
            band["max_s"] = round(max(values), 2)
            band["p95_s"] = round(_p95(values), 2) if len(values) >= P95_MIN_N else None
        out[name] = band
    return out


def report(runs: Sequence[MeetingRun], stats: CallStats, *, expected: int) -> dict:
    """The aggregate report of a gold run, with its SC3 verdict per criterion."""
    scored = [r for r in runs if r.score is not None]
    labels = [lr for r in scored for lr in r.score.labels]  # type: ignore[union-attr]
    missing = [r.fixture for r in runs if r.score is None]
    # Labels of meetings that failed outright count as not found.
    unscored = sum(r.labels_total for r in runs if r.score is None)
    found = [lr for lr in labels if lr.found]
    actions = [lr for lr in found if lr.section == "action_items"]
    needles = [lr for lr in labels if lr.needle]
    long_runs = [r for r in runs if r.duration_min >= 60]
    stored_items = sum(sum(r.score.stored.values()) for r in scored)  # type: ignore[union-attr]
    stored_invalid = sum(r.score.stored_invalid for r in scored)  # type: ignore[union-attr]
    matched = sum(r.score.matched_items for r in scored)  # type: ignore[union-attr]
    supported = sum(r.score.matched_supported for r in scored)  # type: ignore[union-attr]
    cited = sum(r.cited_ids for r in scored)
    invalid = sum(
        r.write["sections"][s]["invalid_ids"] for r in scored for s in r.write["sections"]
    )  # type: ignore[index]
    dropped = {
        s: sum(r.write["sections"][s]["dropped"] for r in scored)  # type: ignore[index]
        for s in ("decisions", "actionItems", "topics", "risks")
    }
    recall = _ratio(len(found), len(labels) + unscored)
    owner_ok = sum(1 for lr in actions if lr.owner_ok)
    unassigned = [lr for lr in actions if lr.owner_expected == UNASSIGNED]
    named = [lr for lr in actions if lr.owner_expected != UNASSIGNED]
    due_ok = sum(1 for lr in actions if lr.due_ok)
    with_due = [lr for lr in actions if lr.due_expected is not None]
    expected_needles = len(long_runs)
    retried = [r for r in runs if r.attempts > 1]
    sc3 = {
        "valid_evidence": {
            "rule": "100% of stored decisions/action items cite valid segment ids",
            "value": _ratio(stored_items - stored_invalid, stored_items),
            "pass": stored_invalid == 0 and not missing,
        },
        "labelled_support": {
            "rule": (
                f"every kept item matching a label cites a labelled segment (±{SUPPORT_WINDOW})"
            ),
            "value": _ratio(supported, matched),
            "pass": supported == matched and not missing,
        },
        "recall": {
            "rule": f"recall >= {RECALL_THRESHOLD:.0%} on labelled decisions and action items",
            "value": recall,
            "pass": recall is not None and recall >= RECALL_THRESHOLD,
        },
        "owners": {
            "rule": "owner (named or unassigned, D-49) exact on every matched labelled action item",
            "value": _ratio(owner_ok, len(actions)),
            "pass": owner_ok == len(actions) and not missing,
        },
        "due_dates": {
            "rule": "due date exact (D-51) on every matched labelled action item",
            "value": _ratio(due_ok, len(actions)),
            "pass": due_ok == len(actions) and not missing,
        },
        "needles": {
            "rule": "every long meeting's minute-45 needle appears in the minutes",
            "value": f"{sum(1 for n in needles if n.found)}/{expected_needles}",
            "pass": sum(1 for n in needles if n.found) == expected_needles,
        },
        "no_truncation": {
            "rule": "no run truncated: every segment in a prompt part, no reply cut by the limit",
            "value": {
                "coverage_failures": sum(1 for r in runs if not r.coverage_ok),
                "truncated_calls": stats.truncated,
            },
            "pass": all(r.coverage_ok for r in runs) and stats.truncated == 0,
        },
    }
    return {
        "processed": len(runs),
        "expected": expected,
        "failed_meetings": [{"fixture": r.fixture, "error": r.error} for r in runs if r.error],
        "retries": {
            "meetings": len(retried),
            "extra_attempts": sum(r.attempts - 1 for r in runs),
            "codes": dict(Counter(e for r in runs for e in r.retry_errors)),
            "retried_calls": stats.retried_calls,
            "wait_s": round(sum(r.retry_wait_s for r in runs), 2),
        },
        "sc3": {**sc3, "pass": all(c["pass"] for c in sc3.values()) and len(runs) == expected},
        "labels": {
            "total": len(labels) + unscored,
            "found": len(found),
            "decisions": _split(labels, "decisions"),
            "action_items": _split(labels, "action_items"),
            "owners": {
                "named_ok": sum(1 for lr in named if lr.owner_ok),
                "named": len(named),
                "unassigned_ok": sum(1 for lr in unassigned if lr.owner_ok),
                "unassigned": len(unassigned),
            },
            "dates": {
                "with_due_ok": sum(1 for lr in with_due if lr.due_ok),
                "with_due": len(with_due),
                "no_due_ok": sum(1 for lr in actions if lr.due_expected is None and lr.due_ok),
                "no_due": sum(1 for lr in actions if lr.due_expected is None),
            },
        },
        "grounding": {
            "cited_ids_before_filter": cited,
            "invalid_ids_before_filter": invalid,
            "invalid_id_rate_before_filter": _ratio(invalid, cited),
            "dropped_items": dropped,
            "stored_items": stored_items,
            "stored_items_with_invalid_ids": stored_invalid,
            "matched_items": matched,
            "matched_items_supported": supported,
        },
        "distractor_hits": {
            "note": (
                "Decisions or action items matching a suggestion nobody agreed to, near where it "
                "was said. An item may rightly record it as parked or rejected: read the drafts. "
                "Informational, not an SC3 criterion."
            ),
            "count": sum(len(r.score.distractor_hits) for r in scored),  # type: ignore[union-attr]
            "by_fixture": {
                r.fixture: r.score.distractor_hits  # type: ignore[union-attr]
                for r in scored
                if r.score.distractor_hits  # type: ignore[union-attr]
            },
        },
        "calls": asdict(stats),
        "schema_failure_rate": _ratio(stats.schema_failures, stats.calls),
        "latency": {
            "bands": bands(runs),
            "claim_limit": (
                "Wall time of generate + writer rules per synthetic meeting through one route on "
                "one date, sequential runs; p95 only where n >= 20."
            ),
        },
        "meetings": [_meeting(r) for r in runs],
    }


def _split(labels: Sequence[LabelResult], section: str) -> dict:
    mine = [lr for lr in labels if lr.section == section]
    return {"found": sum(1 for lr in mine if lr.found), "total": len(mine)}


def _meeting(run: MeetingRun) -> dict:
    out = {
        "fixture": run.fixture,
        "language": run.language,
        "duration_min": run.duration_min,
        "segments": run.segments,
        "seconds": round(run.seconds, 2),
        "parts": run.parts,
        "reduce_calls": run.reduce_calls,
        "prompt_tokens": run.prompt_tokens,
        "completion_tokens": run.completion_tokens,
        "coverage_ok": run.coverage_ok,
        "error": run.error,
        "attempts": run.attempts,
        "retry_errors": run.retry_errors,
        "retry_wait_s": round(run.retry_wait_s, 2),
        "write": run.write,
        "cited_ids": run.cited_ids,
        "draft": run.draft,
    }
    if run.score is not None:
        out["stored"] = run.score.stored
        out["labels"] = [
            {
                "label": lr.label,
                "section": lr.section,
                "found": lr.found,
                "supported": lr.supported,
                **(
                    {
                        "owner": [lr.owner_expected, lr.owner_got],
                        "due": [lr.due_expected, lr.due_got],
                    }
                    if lr.found and lr.section == "action_items"
                    else {}
                ),
                **({"needle": True} if lr.needle else {}),
            }
            for lr in run.score.labels
        ]
        out["distractor_hits"] = run.score.distractor_hits
    return out


def markdown(report_: dict, *, title: str) -> str:
    lines = [f"# {title}", "", f"Processed {report_['processed']} of {report_['expected']}.", ""]
    lines += ["## SC3", "", "| criterion | value | pass |", "|---|---|---|"]
    for name, c in report_["sc3"].items():
        if isinstance(c, dict):
            lines.append(f"| {name}: {c['rule']} | {c['value']} | {'yes' if c['pass'] else 'NO'} |")
    lines += ["", f"Overall: {'pass' if report_['sc3']['pass'] else 'FAIL'}", ""]
    retries = report_["retries"]
    codes = ", ".join(f"{code} x{n}" for code, n in retries["codes"].items()) or "none"
    lines += [
        "## Retries",
        "",
        f"Retries: {retries['meetings']} meeting(s), {retries['extra_attempts']} extra attempt(s),"
        f" {retries['retried_calls']} call(s) in retried attempts, {retries['wait_s']} s waited;"
        f" errors retried: {codes}.",
        "",
    ]
    lines += ["## Latency (s)", "", "| band | n | median | max | p95 |", "|---|---|---|---|---|"]
    for name, b in report_["latency"]["bands"].items():
        lines.append(
            f"| {name} min | {b['n']} | {b.get('median_s')} | {b.get('max_s')} | {b.get('p95_s')} |"
        )
    lines += ["", report_["latency"]["claim_limit"], "", "## Meetings", ""]
    lines += [
        "| fixture | s | parts | found | owner/due misses | attempts | error |",
        "|---|---|---|---|---|---|---|",
    ]
    for m in report_["meetings"]:
        labels = m.get("labels", [])
        found = sum(1 for x in labels if x["found"])
        misses = [
            x["label"]
            for x in labels
            if "owner" in x and (x["owner"][0] != x["owner"][1] or x["due"][0] != x["due"][1])
        ]
        retried = f" ({', '.join(m['retry_errors'])})" if m["retry_errors"] else ""
        lines.append(
            f"| {m['fixture']} ({m['language']}) | {m['seconds']} | {m['parts']} | "
            f"{found}/{len(labels)} | {', '.join(misses)} | {m['attempts']}{retried} | "
            f"{m['error'] or ''} |"
        )
    return "\n".join(lines) + "\n"


def write_report(report_: dict, out_dir: Path, *, stem: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(json.dumps(report_, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    md_path.write_text(markdown(report_, title=f"MoM gold run {stem}"), encoding="utf-8")
    return json_path, md_path


# ─── the whole set ────────────────────────────────────────────────────────────────────────────────


async def run_all(
    llm: ChatJson,
    fixtures: Sequence[Fixture],
    *,
    model: str,
    context_tokens: int,
    max_output_tokens: int,
    part_tokens: int = PART_TOKENS,
    on_meeting: Callable[[MeetingRun], None] | None = None,
    min_calls_for_stop: int = 10,
    attempts: int = RETRY_ATTEMPTS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[list[MeetingRun], CallStats, bool]:
    """Run every fixture in order, retrying transient route errors per meeting. Returns the runs,
    the call counts (every call, retried attempts included) and whether the run stopped early
    because schema failures passed 10% of calls."""
    stats = CallStats()
    counted = Counting(llm, stats)
    runs: list[MeetingRun] = []
    for fixture in fixtures:
        run = await run_fixture(
            counted, fixture, model=model, context_tokens=context_tokens,
            max_output_tokens=max_output_tokens, part_tokens=part_tokens,
            attempts=attempts, sleep=sleep, stats=stats,
        )  # fmt: skip
        runs.append(run)
        if on_meeting is not None:
            on_meeting(run)
        if (
            stats.calls >= min_calls_for_stop
            and stats.schema_failures / stats.calls > SCHEMA_FAILURE_LIMIT
        ):
            return runs, stats, True
    return runs, stats, False


# ─── command line ─────────────────────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m andai_core.minutes.eval validate <gold dir> [fixture.json ...]`."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2 or args[0] != "validate":
        print(main.__doc__)
        return 2
    root = Path(args[1])
    fixtures = [load(Path(p)) for p in args[2:]] if len(args) > 2 else load_all(root)
    problems = [p for f in fixtures for p in validate(f)]
    for p in problems:
        print(p)
    print(json.dumps(count(fixtures)))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "RETRY_ATTEMPTS",
    "RETRY_BASE_S",
    "RETRY_CAP_S",
    "CallStats",
    "Counting",
    "Fixture",
    "MeetingRun",
    "count",
    "dumps",
    "load",
    "load_all",
    "matches",
    "report",
    "run_all",
    "run_fixture",
    "retry_wait",
    "score",
    "transient",
    "validate",
    "write_report",
]
