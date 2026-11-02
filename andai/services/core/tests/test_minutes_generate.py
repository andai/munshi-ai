"""Draft generation (MOM-01..03) with a fake `chat_json`: one call when the transcript fits,
map-reduce when it does not (no segment lost or cut off), every template × output language, and a
draft schema without the fields andai fills itself."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date

import pytest

from andai_core.llm.client import ChatResult, Usage, strict_schema
from andai_core.minutes import templates
from andai_core.minutes.chunking import Segment, estimate_tokens, line, pack
from andai_core.minutes.generate import Draft, Merged, budget, generate
from andai_core.minutes.prompt import messages
from andai_core.minutes.templates import LANGUAGE_INSTRUCTION, LANGUAGES, TEMPLATES

MEETING = date(2026, 11, 2)
ATTENDEES = ["Asha", "Ravi"]
SEG = re.compile(r"^\[(seg_[^\]]+)\] (\S+) ([^:]+): (.*)$")


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
    """Records every call. A map call answers one decision citing the first and last segment of
    its prompt; a reduce call merges everything into one decision naming every reference."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict], type]] = []

    async def __call__(self, msgs: list[dict], schema: type, *, name: str) -> ChatResult:
        self.calls.append((name, msgs, schema))
        user = msgs[-1]["content"]
        if schema is Merged:
            partials = json.loads(user.split("Partial minutes:\n", 1)[1])
            refs = [d["ref"] for p in partials for d in p["decisions"]]
            data = {
                "summary": "merged",
                "decisions": [{"text": "one merged decision", "sources": refs}],
                "actionItems": [],
                "topics": [],
                "risks": [],
            }
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
    llm, segs, *, context_tokens=32000, max_output_tokens=4000, template="standard", language="en"
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
        )  # fmt: skip
    )


def transcript_lines(prompt_text: str) -> list[tuple[str, str]]:
    out = []
    for text in prompt_text.splitlines():
        if m := SEG.match(text):
            out.append((m[1], m[4]))
    return out


# ─── one call or map-reduce ───────────────────────────────────────────────────────────────────────


def test_a_short_transcript_is_one_call() -> None:
    llm = FakeLlm()
    segs = segments(20)

    result = run(llm, segs)

    assert len(llm.calls) == 1 and llm.calls[0][0] == "minutes"
    assert result.parts == 1 and result.reduce_calls == 0
    assert [i for i, _ in transcript_lines(llm.map_prompts()[0])] == [s.id for s in segs]
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
        for seg_id, text in transcript_lines(prompt_text):
            seen[seg_id] = seen.get(seg_id, 0) + 1
            assert text == segs[int(seg_id[4:])].text  # never cut
    assert seen == {s.id: 1 for s in segs}
    # the merged decision's evidence is the union of the partial decisions' evidence
    decision = result.draft.decisions[0]
    assert decision.text == "one merged decision"
    expected = []
    for prompt_text in maps:
        ids = [i for i, _ in transcript_lines(prompt_text)]
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
        for seg_id, text in transcript_lines(p)
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


# ─── the prompt ───────────────────────────────────────────────────────────────────────────────────


def test_lines_carry_id_time_speaker_and_the_raw_text() -> None:
    seg = Segment(id="seg_ab", start_s=125.4, speaker="Asha", text="कल तक  deadline\nहै")
    assert line(seg) == "[seg_ab] 02:05 Asha: कल तक deadline है"
    assert line(Segment(id="seg_x", start_s=5430.0, speaker="R", text="x")) == "[seg_x] 90:30 R: x"


@pytest.mark.parametrize("template_id", sorted(TEMPLATES))
@pytest.mark.parametrize("language", LANGUAGES)
def test_each_template_and_language_has_its_prompt_and_headings(
    template_id: str, language: str
) -> None:
    template = TEMPLATES[template_id]
    msgs = messages(
        ["[seg_1] 00:01 Asha: hello"],
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
    assert "[seg_1] 00:01 Asha: hello" in user
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
