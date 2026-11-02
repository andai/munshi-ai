"""Draft minutes from a stored transcript (MOM-01..03).

`generate(llm, segments, attendees, template, language, meeting_date)` returns a `Generation`
whose `draft` is a `Draft`: the minutes as the model wrote them, before andai checks them. A draft
carries no ids, versions, profile, model or timestamps; `meetings/mom_writer.py` fills those and
applies the evidence, owner and date rules when it stores a version.

A transcript that fits the model's context in one part is one call. A longer one is map-reduce:
one call per part (packed in order by `chunking.pack`, never truncated), then reduce calls that
merge the partial minutes. The reduce model only says which partial items each merged item comes
from (`sources`); andai computes the merged item's evidence as the union of those items' evidence,
and a partial item no merged item names is kept as it was, so no grounded item is lost in the merge.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from andai_core.llm.client import ChatJson, Usage, strict_schema
from andai_core.minutes import prompt
from andai_core.minutes.chunking import Segment, estimate_tokens, pack
from andai_core.minutes.templates import Language, Template
from andai_core.text import fold

# Of the context left after the prompt and the reply, this share is kept free (estimates err).
SAFETY = 0.1
MIN_BUDGET = 256


# ─── the draft (what the model writes) ────────────────────────────────────────────────────────────


class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)


_EVIDENCE = Field(description="Ids of the transcript segments that support this item.")


class DraftDecision(_Draft):
    text: str
    evidence: list[str] = _EVIDENCE


class DraftActionItem(_Draft):
    text: str
    owner: str | None = Field(
        default=None, description="Who will do it, only if the transcript clearly names them."
    )
    owner_confidence: float | None = Field(
        default=None, description="0 to 1: how sure the transcript names this owner."
    )
    due_text: str | None = Field(default=None, description="The due phrase exactly as said.")
    due: str | None = Field(default=None, description="YYYY-MM-DD, only when sure.")
    evidence: list[str] = _EVIDENCE


class DraftTopic(_Draft):
    title: str
    evidence: list[str] = _EVIDENCE


class DraftRisk(_Draft):
    text: str
    evidence: list[str] = _EVIDENCE


class Draft(_Draft):
    summary: str
    decisions: list[DraftDecision]
    action_items: list[DraftActionItem]
    topics: list[DraftTopic]
    risks: list[DraftRisk]


# ─── the reduce reply (merged items name their partial items) ─────────────────────────────────────

_SOURCES = Field(description="References of the partial items this item merges, e.g. D1.2.")


class MergedDecision(_Draft):
    text: str
    sources: list[str] = _SOURCES


class MergedActionItem(_Draft):
    text: str
    owner: str | None = None
    owner_confidence: float | None = None
    due_text: str | None = None
    due: str | None = None
    sources: list[str] = _SOURCES


class MergedTopic(_Draft):
    title: str
    sources: list[str] = _SOURCES


class MergedRisk(_Draft):
    text: str
    sources: list[str] = _SOURCES


class Merged(_Draft):
    summary: str
    decisions: list[MergedDecision]
    action_items: list[MergedActionItem]
    topics: list[MergedTopic]
    risks: list[MergedRisk]


# section field, the letter of its partial-item references
SECTIONS: tuple[tuple[str, str], ...] = (
    ("decisions", "D"),
    ("action_items", "A"),
    ("topics", "T"),
    ("risks", "R"),
)


@dataclass
class Generation:
    draft: Draft
    usage: Usage = field(default_factory=Usage)
    parts: int = 1
    reduce_calls: int = 0


# ─── generate ─────────────────────────────────────────────────────────────────────────────────────


def budget(
    template: Template,
    language: Language,
    attendees: Sequence[str],
    meeting_date: date,
    *,
    context_tokens: int,
    max_output_tokens: int,
) -> int:
    """Estimated tokens of transcript one call may carry."""
    fixed = estimate_tokens(prompt.overhead(template, language, attendees, meeting_date))
    fixed += estimate_tokens(json.dumps(strict_schema(Draft)))
    room = int((context_tokens - max_output_tokens - fixed) * (1 - SAFETY))
    if room < MIN_BUDGET:
        raise ValueError("the model's context is too small for minutes (ANDAI_LLM_CONTEXT_TOKENS)")
    return room


async def generate(
    llm: ChatJson,
    segments: Sequence[Segment],
    attendees: Sequence[str],
    template: Template,
    language: Language,
    meeting_date: date,
    *,
    context_tokens: int = 32000,
    max_output_tokens: int = 4000,
) -> Generation:
    """Draft minutes for a transcript. Raises `ValueError` for an empty transcript and the
    client's typed `LlmError`s when a call fails (nothing partial is returned)."""
    if not segments:
        raise ValueError("an empty transcript has no minutes")
    room = budget(
        template, language, attendees, meeting_date,
        context_tokens=context_tokens, max_output_tokens=max_output_tokens,
    )  # fmt: skip
    parts = pack(segments, room)
    usage = Usage()
    drafts: list[Draft] = []
    for index, lines in enumerate(parts, start=1):
        result = await llm(
            prompt.messages(
                lines,
                attendees=attendees,
                template=template,
                language=language,
                meeting_date=meeting_date,
                part=(index, len(parts)),
            ),
            Draft,
            name="minutes" if len(parts) == 1 else "minutes_part",
        )
        usage.add(result.usage)
        drafts.append(Draft.model_validate(result.data))
    reduce_calls = 0
    while len(drafts) > 1:
        merged: list[Draft] = []
        for group in _groups(drafts, room):
            if len(group) == 1:
                merged.append(group[0])
                continue
            result = await llm(
                prompt.reduce_messages(
                    [_referenced(i, d) for i, d in enumerate(group, start=1)],
                    attendees=attendees,
                    template=template,
                    language=language,
                    meeting_date=meeting_date,
                ),
                Merged,
                name="minutes_merge",
            )
            usage.add(result.usage)
            reduce_calls += 1
            merged.append(_resolve(Merged.model_validate(result.data), group))
        if len(merged) == len(drafts):  # every draft fills a group alone: concatenate them
            merged = [_concat(drafts)]
        drafts = merged
    return Generation(drafts[0], usage, parts=len(parts), reduce_calls=reduce_calls)


# ─── reduce helpers ───────────────────────────────────────────────────────────────────────────────


def _referenced(part: int, draft: Draft) -> dict:
    """A partial draft as the reduce prompt shows it: each item with its reference."""
    out: dict = {"part": part, "summary": draft.summary}
    for section, letter in SECTIONS:
        items = []
        for i, item in enumerate(getattr(draft, section), start=1):
            body = item.model_dump(by_alias=True, exclude={"evidence"}, exclude_none=True)
            items.append({"ref": f"{letter}{part}.{i}", **body})
        out[to_camel(section)] = items
    return out


def _groups(drafts: list[Draft], room: int) -> list[list[Draft]]:
    """Consecutive drafts grouped so each group's reduce prompt fits the budget."""
    groups: list[list[Draft]] = []
    current: list[Draft] = []
    size = 0
    for d in drafts:
        cost = estimate_tokens(json.dumps(_referenced(99, d), ensure_ascii=False))
        if current and size + cost > room:
            groups.append(current)
            current, size = [], 0
        current.append(d)
        size += cost
    groups.append(current)
    return groups


def _union(lists: Sequence[Sequence[str]]) -> list[str]:
    seen: dict[str, None] = {}
    for evidence in lists:
        for segment_id in evidence:
            seen.setdefault(segment_id, None)
    return list(seen)


def _key(section: str, item: BaseModel) -> str:
    return fold(getattr(item, "title", None) or getattr(item, "text", ""))


def _resolve(merged: Merged, group: list[Draft]) -> Draft:
    """The merged draft with evidence = the union of each merged item's sources' evidence; partial
    items no merged item names are appended unchanged; items with the same text are combined."""
    sections: dict[str, list] = {}
    for section, letter in SECTIONS:
        partial = {
            f"{letter}{part}.{i}": item
            for part, d in enumerate(group, start=1)
            for i, item in enumerate(getattr(d, section), start=1)
        }
        used: set[str] = set()
        cls = _draft_class(section)
        items: list = []
        for item in getattr(merged, section):
            refs = [r.strip() for r in item.sources if r.strip() in partial]
            used.update(refs)
            fields = item.model_dump(exclude={"sources"})
            evidence = _union([partial[r].evidence for r in refs])
            items.append(cls.model_validate({**fields, "evidence": evidence}))
        items.extend(item for ref, item in partial.items() if ref not in used)
        sections[section] = _dedupe(section, items)
    return Draft(summary=merged.summary, **sections)


def _draft_class(section: str) -> type[BaseModel]:
    return {
        "decisions": DraftDecision,
        "action_items": DraftActionItem,
        "topics": DraftTopic,
        "risks": DraftRisk,
    }[section]


def _dedupe(section: str, items: list) -> list:
    out: dict[str, BaseModel] = {}
    for item in items:
        key = _key(section, item)
        if key in out:
            kept = out[key]
            kept.evidence = _union([kept.evidence, item.evidence])  # type: ignore[attr-defined]
        else:
            out[key] = item.model_copy()
    return list(out.values())


def _concat(drafts: list[Draft]) -> Draft:
    return Draft(
        summary=" ".join(d.summary for d in drafts if d.summary),
        **{
            section: _dedupe(section, [i for d in drafts for i in getattr(d, section)])
            for section, _ in SECTIONS
        },
    )


__all__ = [
    "Draft",
    "DraftActionItem",
    "DraftDecision",
    "DraftRisk",
    "DraftTopic",
    "Generation",
    "Merged",
    "budget",
    "generate",
]
