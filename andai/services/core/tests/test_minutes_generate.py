"""Draft generation (MOM-01..03) with a fake `chat_json`: one call when the transcript fits,
map-reduce when it does not (no segment lost or cut off, every call within the part budget), line
numbers mapped back to segment ids, merge groups combined by andai, every template × output
language, and a draft schema without the fields andai fills itself."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date

import pytest

from andai_core.llm.client import ChatResult, Usage, strict_schema
from andai_core.minutes import templates
from andai_core.minutes.chunking import Segment, estimate_tokens, line, pack, refs
from andai_core.minutes.generate import PART_TOKENS, Draft, Merged, budget, generate
from andai_core.minutes.prompt import messages
from andai_core.minutes.templates import LANGUAGE_INSTRUCTION, LANGUAGES, TEMPLATES

MEETING = date(2026, 11, 2)
ATTENDEES = ["Asha", "Ravi"]
SEG = re.compile(r"^\[(\d+)\] (\S+) ([^:]+): (.*)$")
ITEM = re.compile(r"^([DATR]\d+\.\d+) ", re.MULTILINE)


def segments(
    n: int, text: str = "budget review ke baare mein baat hui, vendor se call karna hai"
) -> list[Segment]:
    return [
        Segment(
            id=f"seg_{i:04d}",
            start_s=i * 7.5,
            speaker="Asha" if i % 2 else "Ravi",
            text=f"{text} {i}",
        )
        for i in range(n)
    ]


class FakeLlm:
    """Records every call. A map call answers one decision citing the first and last line number of
    its prompt; a reduce call groups every decision into one."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict], type]] = []

    async def __call__(self, msgs: list[dict], schema: type, *, name: str) -> ChatResult:
        self.calls.append((name, msgs, schema))
        user = msgs[-1]["content"]
        if schema is Merged:
            decisions = [r for r in ITEM.findall(user) if r.startswith("D")]
            data = {"summary": "merged", "groups": [{"sources": decisions}]}
        else:
            ids = [m[1] for m in (SEG.match(x) for x in user.splitlines()) if m]
            data = {
                "summary": f"part with {len(ids)} lines",
                "decisions": [{"text": f"decision {ids[0]}", "evidence": [ids[0], ids[-1]]}],
                "actionItems": [
                    {
                        "text": "send the sheet",
                        "owner": "Ravi",
                        "ownerConfidence": 0.9,
                        "dueText": "kal tak",
                        "due": None,
                        "evidence": [ids[0]],
                    }
                ],
                "topics": [{"title": "Budget", "evidence": [ids[-1]]}],
                "risks": [],
            }
        return ChatResult(data, Usage(prompt_tokens=10, completion_tokens=5, calls=1))

    def map_prompts(self) -> list[str]:
        return [m[-1]["content"] for name, m, s in self.calls if s is Draft]


def run(
    llm,
    segs,
    *,
    context_tokens=32000,
    max_output_tokens=4000,
    part_tokens=PART_TOKENS,
    template="standard",
    language="en",
):
    return asyncio.run(
        generate(
            llm,
            segs,
            ATTENDEES,
            TEMPLATES[template],
            language,
            MEETING,
            context_tokens=context_tokens,
            max_output_tokens=max_output_tokens,
            part_tokens=part_tokens,
        )  # fmt: skip
    )


def transcript_lines(prompt_text: str, segs: list[Segment]) -> list[tuple[str, str]]:
    """(segment id, text) of each transcript line in a prompt, the line number mapped back."""
    numbers = refs(segs)
    out = []
    for text in prompt_text.splitlines():
        if m := SEG.match(text):
            out.append((numbers[m[1]], m[4]))
    return out


# ─── one call or map-reduce ───────────────────────────────────────────────────────────────────────


def test_a_short_transcript_is_one_call() -> None:
    llm = FakeLlm()
    segs = segments(20)

    result = run(llm, segs)

    assert len(llm.calls) == 1 and llm.calls[0][0] == "minutes"
    assert result.parts == 1 and result.reduce_calls == 0
    assert [i for i, _ in transcript_lines(llm.map_prompts()[0], segs)] == [s.id for s in segs]
    assert "[1] " in llm.map_prompts()[0] and "seg_" not in llm.map_prompts()[0]
    assert result.draft.decisions[0].evidence == ["seg_0000", "seg_0019"]
    assert result.usage.calls == 1


def test_a_long_transcript_is_map_reduce_without_losing_a_segment() -> None:
    llm = FakeLlm()
    segs = segments(400)

    result = run(llm, segs, context_tokens=6000, max_output_tokens=1000)

    maps = llm.map_prompts()
    assert len(maps) > 1
    assert len(llm.calls) == len(maps) + 1  # N map calls + 1 reduce
    assert llm.calls[-1][0] == "minutes_merge"
    seen: dict[str, int] = {}
    for prompt_text in maps:
        assert f"of {len(maps)} of the transcript" in prompt_text
        for seg_id, text in transcript_lines(prompt_text, segs):
            seen[seg_id] = seen.get(seg_id, 0) + 1
            assert text == segs[int(seg_id[4:])].text  # never cut
    assert seen == {s.id: 1 for s in segs}
    # the grouped decisions become one, with the first's text and the union of their evidence
    assert len(result.draft.decisions) == 1
    decision = result.draft.decisions[0]
    assert decision.text == "decision 1"
    expected = []
    for prompt_text in maps:
        ids = [i for i, _ in transcript_lines(prompt_text, segs)]
        expected += [ids[0], ids[-1]]
    assert decision.evidence == list(dict.fromkeys(expected))
    # items the reduce did not name are kept, and same-text items are combined
    assert [a.text for a in result.draft.action_items] == ["send the sheet"]
    assert len(result.draft.action_items[0].evidence) == len(maps)
    assert result.parts == len(maps) and result.reduce_calls == 1
    assert result.usage.calls == len(maps) + 1


def test_every_part_stays_within_the_budget() -> None:
    room = budget(
        TEMPLATES["standard"], "en", ATTENDEES, MEETING, context_tokens=6000, max_output_tokens=1000
    )
    parts = pack(segments(400), room)
    assert len(parts) > 1
    for lines in parts:
        assert sum(estimate_tokens(x) + 1 for x in lines) <= room


def test_an_over_long_segment_is_split_keeping_its_id() -> None:
    llm = FakeLlm()
    long_text = " ".join(f"शब्द{i} word{i}" for i in range(3000))
    segs = [
        *segments(3),
        Segment(id="seg_long", start_s=99.0, speaker="Asha", text=long_text),
        *segments(2),
    ]

    run(llm, segs, context_tokens=6000, max_output_tokens=1000)

    pieces = [
        text
        for p in llm.map_prompts()
        for seg_id, text in transcript_lines(p, segs)
        if seg_id == "seg_long"
    ]
    assert len(pieces) > 1
    assert " ".join(pieces) == long_text


def test_an_empty_transcript_has_no_minutes() -> None:
    with pytest.raises(ValueError):
        run(FakeLlm(), [])


def test_a_context_too_small_is_refused() -> None:
    with pytest.raises(ValueError):
        run(FakeLlm(), segments(3), context_tokens=1500, max_output_tokens=1000)


def test_a_failing_call_returns_nothing() -> None:
    from andai_core.llm import LlmInvalidOutput

    async def broken(msgs, schema, *, name):
        raise LlmInvalidOutput("bad")

    with pytest.raises(LlmInvalidOutput):
        run(broken, segments(3))


def test_cited_numbers_become_segment_ids_and_unknown_ones_stay_as_written() -> None:
    async def citing(msgs, schema, *, name):
        data = {
            "summary": "s",
            "decisions": [{"text": "d", "evidence": ["2", "[3]", " 2 ", "77", "seg_9999"]}],
            "actionItems": [],
            "topics": [],
            "risks": [],
        }
        return ChatResult(data, Usage(calls=1))

    result = run(citing, segments(5))

    # 77 names no line and an id was never shown: both stay as written, for the writer to drop
    assert result.draft.decisions[0].evidence == ["seg_0001", "seg_0002", "77", "seg_9999"]


def test_part_tokens_bounds_every_call() -> None:
    llm = FakeLlm()
    segs = segments(600)

    result = run(llm, segs, part_tokens=1500)

    room = budget(
        TEMPLATES["standard"], "en", ATTENDEES, MEETING,
        context_tokens=32000, max_output_tokens=4000, part_tokens=1500,
    )  # fmt: skip
    assert room == 1500
    assert result.parts > 1
    for name, msgs, _ in llm.calls:
        body = msgs[-1]["content"]
        carried = (
            body.split("Partial minutes:", 1)[1]
            if name == "minutes_merge"
            else "\n".join(x for x in body.splitlines() if SEG.match(x))
        )
        assert estimate_tokens(carried) <= room + 50, name


def test_merge_groups_combine_owner_due_and_evidence_and_keep_the_rest() -> None:
    def part(owner, conf, due, evidence, extra):
        return {
            "summary": "p",
            "decisions": [],
            "actionItems": [
                {"text": f"send the sheet {extra}", "owner": owner, "ownerConfidence": conf,
                 "dueText": due, "due": None, "evidence": evidence},
                {"text": f"other {extra}", "owner": None, "ownerConfidence": None,
                 "dueText": None, "due": None, "evidence": evidence},
            ],
            "topics": [{"title": f"topic {extra}", "evidence": evidence}],
            "risks": [],
        }  # fmt: skip

    replies = iter(
        [
            part("Asha", 0.7, None, ["1"], "a"),
            part("Ravi", 0.9, "kal tak", ["600"], "b"),
        ]
    )
    merges: list[str] = []

    async def llm(msgs, schema, *, name):
        if schema is Merged:
            merges.append(msgs[-1]["content"])
            groups = [
                {"sources": ["A1.1", "A2.1"]},  # the same task in both parts
                {"sources": ["A1.2", "T2.1"]},  # two kinds: not combined
                {"sources": ["T1.1", "Z9.9"]},  # one known reference: not a group
            ]
            return ChatResult({"summary": "whole", "groups": groups}, Usage(calls=1))
        return ChatResult(next(replies), Usage(calls=1))

    segs = segments(600)
    result = asyncio.run(
        generate(llm, segs, ATTENDEES, TEMPLATES["standard"], "en", MEETING, part_tokens=12000)
    )

    assert result.parts == 2 and result.reduce_calls == 1
    assert "A1.1 send the sheet a (owner: Asha)" in merges[0]
    assert "A2.1 send the sheet b (owner: Ravi; due: kal tak)" in merges[0]
    draft = result.draft
    assert draft.summary == "whole"
    sheet = draft.action_items[0]
    assert sheet.text == "send the sheet a"
    assert (sheet.owner, sheet.owner_confidence, sheet.due_text) == ("Ravi", 0.9, "kal tak")
    assert sheet.evidence == ["seg_0000", "seg_0599"]
    assert [a.text for a in draft.action_items] == ["send the sheet a", "other a", "other b"]
    assert [t.title for t in draft.topics] == ["topic a", "topic b"]


# ─── the prompt ───────────────────────────────────────────────────────────────────────────────────


def test_lines_carry_number_time_speaker_and_the_raw_text() -> None:
    seg = Segment(id="seg_ab", start_s=125.4, speaker="Asha", text="कल तक  deadline\nहै")
    assert line(seg, "7") == "[7] 02:05 Asha: कल तक deadline है"
    assert (
        line(Segment(id="seg_x", start_s=5430.0, speaker="R", text="x"), "981")
        == "[981] 90:30 R: x"
    )
    assert refs(segments(3)) == {"1": "seg_0000", "2": "seg_0001", "3": "seg_0002"}


@pytest.mark.parametrize("template_id", sorted(TEMPLATES))
@pytest.mark.parametrize("language", LANGUAGES)
def test_each_template_and_language_has_its_prompt_and_headings(
    template_id: str, language: str
) -> None:
    template = TEMPLATES[template_id]
    msgs = messages(
        ["[1] 00:01 Asha: hello"],
        attendees=ATTENDEES,
        template=template,
        language=language,  # type: ignore[arg-type]
        meeting_date=MEETING,
    )
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert LANGUAGE_INSTRUCTION[language] in system  # type: ignore[index]
    assert template.guidance in system
    assert "evidence" in system and "ownerConfidence" in system and "dueText" in system
    assert "Meeting date: 2026-11-02 (Monday)" in user
    assert "Attendees: Asha, Ravi" in user
    assert "[1] 00:01 Asha: hello" in user
    assert set(template.sections) <= {
        "summary",
        "attendees",
        "topics",
        "decisions",
        "action_items",
        "risks",
    }
    for section in ("summary", "topics", "decisions", "action_items", "risks"):
        assert section in template.sections
        assert template.heading(section, language).strip()  # type: ignore[arg-type]
    assert template.name(language).strip()  # type: ignore[arg-type]
    if language == "hi":
        assert any("ऀ" <= c <= "ॿ" for c in template.heading("decisions", "hi"))
    if language == "hinglish":
        assert not any(
            "ऀ" <= c <= "ॿ" for s in template.sections for c in template.heading(s, "hinglish")
        )


def test_four_templates_ship() -> None:
    assert set(TEMPLATES) == {"standard", "indian_formal_mom", "standup", "client_call"}
    assert templates.get("standup") is TEMPLATES["standup"]
    with pytest.raises(ValueError):
        templates.get("nope")


@pytest.mark.parametrize("language", LANGUAGES)
def test_generation_runs_for_each_language(language: str) -> None:
    llm = FakeLlm()
    run(llm, segments(5), language=language)
    assert LANGUAGE_INSTRUCTION[language] in llm.calls[0][1][0]["content"]


# ─── the draft schema ─────────────────────────────────────────────────────────────────────────────


def test_the_draft_schema_has_no_fields_andai_fills() -> None:
    text = json.dumps(strict_schema(Draft))
    for name in (
        "id",
        "version",
        "meetingId",
        "llmProfileId",
        "model",
        "generatedAt",
        "editedBy",
        "templateId",
        "outputLanguage",
    ):
        assert f'"{name}"' not in text, name
    props = strict_schema(Draft)["properties"]
    assert set(props) == {"summary", "decisions", "actionItems", "topics", "risks"}
    for name in ("owner", "ownerConfidence", "dueText", "due", "evidence"):
        assert f'"{name}"' in text
