"""The minutes gold set offline (MOM-02, D-45): every fixture is sound, the set has the planned
shape, the long meetings regenerate byte for byte, and the evaluator (`minutes.eval`) scores a
perfect answer 100% and doctored answers below threshold. No language model is called: a fake
`chat_json` answers with the labels."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from andai_core.llm.client import ChatResult, LlmInvalidOutput, Usage
from andai_core.minutes import eval as gold
from andai_core.minutes.generate import Merged
from andai_core.minutes.owners import UNASSIGNED

ROOT = Path(__file__).resolve().parent / "mom_gold"
FIXTURES = gold.load_all(ROOT)
LINE_ID = re.compile(r"^\[(seg_[0-9a-f]+)\] ")
CONTEXT = 32000
MAX_OUTPUT = 4000
BOGUS = "seg_bogus"


def _build_long():
    spec = importlib.util.spec_from_file_location("build_long", ROOT / "build_long.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look the module up
    spec.loader.exec_module(module)
    return module


# ─── the set ──────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f"{f.language}/{f.id}")
def test_every_fixture_is_sound(fixture: gold.Fixture) -> None:
    assert gold.validate(fixture) == []


def test_the_set_has_the_planned_shape() -> None:
    totals = gold.count(FIXTURES)
    assert (totals["en"], totals["hi"], totals["hinglish"], totals["total"]) == (14, 13, 13, 40)
    assert (totals["long"], totals["long_90"], totals["needles"]) == (6, 2, 6)
    long = {(f.language, f.duration_min) for f in FIXTURES if f.kind == "long"}
    per = {
        lang: sorted(f.duration_min for f in FIXTURES if f.kind == "long" and f.language == lang)
        for lang in gold.LANGUAGES
    }
    assert per == {"en": [60, 90], "hi": [60, 90], "hinglish": [60, 60]}
    assert len(long) == 5
    for f in FIXTURES:
        if f.kind == "long" and f.duration_min == 90:
            assert len(f.segments) >= 900
    short = [f for f in FIXTURES if f.kind == "short"]
    assert [sum(1 for f in short if f.language == lang) for lang in gold.LANGUAGES] == [12, 11, 11]


def test_labelled_evidence_names_segments_of_its_fixture() -> None:
    for f in FIXTURES:
        ids = {s["id"] for s in f.segments}
        for section in ("decisions", "action_items", "distractors"):
            for label in f.labels.get(section, []):
                assert label["evidence"] and set(label["evidence"]) <= ids, (f.id, label["id"])


def test_hindi_speech_is_devanagari_and_hinglish_is_mostly_roman() -> None:
    def devanagari_share(f: gold.Fixture) -> float:
        text = "".join(s["text"] for s in f.segments)
        letters = [c for c in text if c.isalpha()]
        return sum(1 for c in letters if "ऀ" <= c <= "ॿ") / len(letters)

    assert all(devanagari_share(f) > 0.7 for f in FIXTURES if f.language == "hi")
    assert all(devanagari_share(f) < 0.5 for f in FIXTURES if f.language == "hinglish")
    assert sum(1 for f in FIXTURES if f.language == "hinglish" and devanagari_share(f) > 0) >= 2


def test_the_long_meetings_regenerate_byte_for_byte() -> None:
    built = _build_long().build()
    assert len(built) == 6
    for path, text in built.items():
        assert path.read_text(encoding="utf-8") == text, path.name


def test_fixture_ids_become_production_shaped_segment_ids() -> None:
    f = FIXTURES[0]
    ids = [s.id for s in f.transcript()]
    assert all(re.fullmatch(r"seg_[0-9a-f]{24}", i) for i in ids)
    assert len(set(ids)) == len(ids)
    assert ids == [s.id for s in f.transcript()]  # stable


# ─── matching ─────────────────────────────────────────────────────────────────────────────────────


def test_matching_folds_scripts_and_keeps_short_words_strict() -> None:
    assert gold.matches([["रिपोर्ट", "report"]], "Kabir report kal tak bhejega")
    assert gold.matches([["बजट"], ["15"]], "बजट 15 दिसंबर तक")
    assert gold.matches([["scorecard"]], "Draft the courier scorecards")  # a prefix
    assert gold.matches([["checklist"]], "a check-list for the camps") is False
    assert gold.matches([["state"]], "हम असहमत रह सकते हैं") is False  # "sakte" is not "state"
    assert gold.matches([["499"]], "4999 rupees") is False  # numbers exactly
    assert gold.matches([["website"], ["pricing"]], "update the website") is False  # every group


# ─── the evaluator ────────────────────────────────────────────────────────────────────────────────


def _item_text(label: dict) -> str:
    return " ".join(group[0] for group in label["match"])


def perfect(
    f: gold.Fixture, change: Callable[[str, dict, dict], dict | None] | None = None
) -> list[tuple[str, dict]]:
    """(section, draft item) answering every labelled decision and action item exactly;
    `change(section, label, item)` may alter or drop (None) an item."""
    items = []
    for section, label in f.labelled():
        item: dict = {
            "text": _item_text(label),
            "evidence": [f.global_id(e) for e in label["evidence"]],
        }
        if section == "action_items":
            named = label["owner"] != UNASSIGNED
            item |= {
                "owner": label["owner"] if named else None,
                "ownerConfidence": 0.9 if named else None,
                "dueText": label.get("due_phrase"),
                "due": None,
            }
        if change is not None:
            item = change(section, label, item)
        if item is not None:
            items.append((section, item))
    return items


class Answering:
    """A fake `chat_json`: map calls answer the items whose evidence is in the part (evidence cut
    to the part); reduce calls keep every partial item as its own merged item."""

    def __init__(self, items: list[tuple[str, dict]]) -> None:
        self.items = items
        self.invented: set[int] = set()

    async def __call__(self, messages: list[dict], schema, *, name: str) -> ChatResult:
        user = messages[-1]["content"]
        if schema is Merged:
            partials = json.loads(user.split("Partial minutes:\n", 1)[1])
            data: dict = {"summary": "merged", "topics": [], "risks": []}
            for key in ("decisions", "actionItems"):
                data[key] = [
                    {**{k: v for k, v in item.items() if k != "ref"}, "sources": [item["ref"]]}
                    for p in partials
                    for item in p[key]
                ]
            return ChatResult(data, Usage(prompt_tokens=10, completion_tokens=5, calls=1))
        present = {m.group(1) for m in map(LINE_ID.match, user.splitlines()) if m}
        data = {
            "summary": "a meeting",
            "decisions": [],
            "actionItems": [],
            "topics": [],
            "risks": [],
        }
        for index, (section, item) in enumerate(self.items):
            cited = [e for e in item["evidence"] if e in present]
            if item["evidence"] == [BOGUS] and index not in self.invented:
                cited = [BOGUS]  # an invented id, cited once
                self.invented.add(index)
            if cited:
                key = "decisions" if section == "decisions" else "actionItems"
                data[key].append({**item, "evidence": cited})
        return ChatResult(data, Usage(prompt_tokens=10, completion_tokens=5, calls=1))


def run(f: gold.Fixture, items, stats: gold.CallStats | None = None) -> gold.MeetingRun:
    llm = Answering(items)
    if stats is not None:
        llm = gold.Counting(llm, stats)
    return asyncio.run(
        gold.run_fixture(llm, f, model="fake", context_tokens=CONTEXT, max_output_tokens=MAX_OUTPUT)
    )


def test_a_perfect_answer_scores_100_percent_on_the_whole_set() -> None:
    stats = gold.CallStats()
    runs = [run(f, perfect(f), stats) for f in FIXTURES]
    report = gold.report(runs, stats, expected=40)

    assert report["processed"] == 40
    sc3 = report["sc3"]
    assert sc3["pass"], json.dumps(sc3, indent=1)
    assert sc3["recall"]["value"] == 1.0
    assert sc3["valid_evidence"]["value"] == 1.0
    assert sc3["labelled_support"]["value"] == 1.0
    assert sc3["owners"]["value"] == 1.0 and sc3["due_dates"]["value"] == 1.0
    assert sc3["needles"]["value"] == "6/6"
    assert report["grounding"]["invalid_ids_before_filter"] == 0
    assert report["distractor_hits"]["count"] == 0
    assert all(r.coverage_ok for r in runs)
    bands = report["latency"]["bands"]
    assert [bands[b]["n"] for b in ("<=30", "31-60", "61-90")] == [34, 4, 2]
    assert bands["<=30"]["p95_s"] is not None
    assert bands["31-60"]["p95_s"] is None and bands["61-90"]["p95_s"] is None


SHORT = next(f for f in FIXTURES if f.kind == "short" and f.language == "en")
NEEDLED = next(f for f in FIXTURES if f.kind == "long" and f.language == "hinglish")


def _verdict(f: gold.Fixture, change) -> dict:
    stats = gold.CallStats()
    return gold.report([run(f, perfect(f, change), stats)], stats, expected=1)


def test_a_long_meeting_beyond_the_context_is_map_reduced_and_still_scores_100() -> None:
    stats = gold.CallStats()
    for f in [f for f in FIXTURES if f.kind == "long"]:
        result = asyncio.run(
            gold.run_fixture(
                gold.Counting(Answering(perfect(f)), stats), f, model="fake",
                context_tokens=9000, max_output_tokens=MAX_OUTPUT,
            )
        )  # fmt: skip
        report = gold.report([result], stats, expected=1)
        assert result.parts > 2 and result.reduce_calls >= 1 and result.coverage_ok, f.id
        assert report["sc3"]["pass"], (f.id, report["sc3"])


def test_an_invented_evidence_id_is_dropped_and_counted() -> None:
    def invent(section, label, item):
        return {**item, "evidence": [BOGUS]} if label["id"] == "D1" else item

    report = _verdict(SHORT, invent)

    assert report["grounding"]["invalid_ids_before_filter"] == 1
    assert report["grounding"]["dropped_items"]["decisions"] == 1
    assert report["sc3"]["recall"]["value"] < 1.0
    assert report["sc3"]["valid_evidence"]["pass"]  # what is stored stays grounded


def test_a_wrong_owner_fails_the_owner_criterion() -> None:
    named = next(a for a in SHORT.labels["action_items"] if a["owner"] != UNASSIGNED)
    other = next(p for p in SHORT.attendees if p != named["owner"])

    def swap(section, label, item):
        return {**item, "owner": other} if label["id"] == named["id"] else item

    report = _verdict(SHORT, swap)
    assert not report["sc3"]["owners"]["pass"] and not report["sc3"]["pass"]


def test_a_named_owner_for_an_unassigned_item_fails() -> None:
    unassigned = next(a for a in SHORT.labels["action_items"] if a["owner"] == UNASSIGNED)

    def name(section, label, item):
        if label["id"] != unassigned["id"]:
            return item
        return {**item, "owner": SHORT.attendees[0], "ownerConfidence": 0.95}

    assert not _verdict(SHORT, name)["sc3"]["owners"]["pass"]


def test_a_wrong_date_fails_the_date_criterion() -> None:
    dated = next(a for a in SHORT.labels["action_items"] if a.get("due"))

    def shift(section, label, item):
        return {**item, "dueText": "in 30 days"} if label["id"] == dated["id"] else item

    report = _verdict(SHORT, shift)
    assert not report["sc3"]["due_dates"]["pass"] and not report["sc3"]["pass"]


def test_a_missing_needle_fails_the_needle_criterion() -> None:
    report = _verdict(NEEDLED, lambda section, label, item: None if label.get("needle") else item)
    assert report["sc3"]["needles"]["value"] == "0/1"
    assert not report["sc3"]["needles"]["pass"] and not report["sc3"]["pass"]


def test_an_item_citing_far_away_segments_is_unsupported() -> None:
    far = SHORT.segments[-1]["id"]

    def move(section, label, item):
        return {**item, "evidence": [SHORT.global_id(far)]} if label["id"] == "D1" else item

    report = _verdict(SHORT, move)
    assert report["sc3"]["labelled_support"]["value"] < 1.0
    assert not report["sc3"]["labelled_support"]["pass"]


def test_a_decision_from_a_suggestion_nobody_agreed_is_reported() -> None:
    dropped = SHORT.labels["distractors"][0]
    assert dropped["kind"] == "not_agreed"
    items = perfect(SHORT) + [
        (
            "decisions",
            {
                "text": _item_text(dropped),
                "evidence": [SHORT.global_id(e) for e in dropped["evidence"]],
            },
        )
    ]
    stats = gold.CallStats()
    report = gold.report([run(SHORT, items, stats)], stats, expected=1)
    assert report["distractor_hits"]["count"] == 1


def test_a_meeting_that_fails_counts_its_labels_as_missed_and_fails_sc3() -> None:
    class Broken:
        async def __call__(self, messages, schema, *, name):
            raise LlmInvalidOutput("off schema")

    stats = gold.CallStats()
    failed = asyncio.run(
        gold.run_fixture(
            gold.Counting(Broken(), stats), SHORT, model="fake",
            context_tokens=CONTEXT, max_output_tokens=MAX_OUTPUT,
        )
    )  # fmt: skip
    good = run(FIXTURES[1], perfect(FIXTURES[1]))
    report = gold.report([failed, good], stats, expected=2)

    assert failed.error == "llm_invalid_output"
    assert report["failed_meetings"] == [{"fixture": SHORT.id, "error": "llm_invalid_output"}]
    assert report["sc3"]["recall"]["value"] < 1.0
    assert not report["sc3"]["pass"]
    assert stats.schema_failures == 1


def test_the_run_stops_when_schema_failures_pass_ten_percent() -> None:
    class Broken:
        async def __call__(self, messages, schema, *, name):
            raise LlmInvalidOutput("off schema")

    runs, stats, stopped = asyncio.run(
        gold.run_all(
            Broken(), FIXTURES, model="fake", context_tokens=CONTEXT,
            max_output_tokens=MAX_OUTPUT,
        )
    )  # fmt: skip
    assert stopped and len(runs) == 10 and stats.schema_failures == 10


def test_the_report_files_name_no_route(tmp_path: Path) -> None:
    stats = gold.CallStats()
    report = gold.report([run(SHORT, perfect(SHORT), stats)], stats, expected=1)
    json_path, md_path = gold.write_report(report, tmp_path, stem="gold-test")

    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["sc3"]["pass"] is True
    text = json_path.read_text(encoding="utf-8") + md_path.read_text(encoding="utf-8")
    assert "http" not in text and "Bearer" not in text
    assert "| recall:" in md_path.read_text(encoding="utf-8")
