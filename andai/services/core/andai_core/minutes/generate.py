"""Draft minutes from a stored transcript (MOM-01..03).

`generate(llm, segments, attendees, template, language, meeting_date)` returns a `Generation`
whose `draft` is a `Draft`: the minutes as the model wrote them, before andai checks them. A draft
carries no ids, versions, profile, model or timestamps; `meetings/mom_writer.py` fills those and
applies the evidence, owner and date rules when it stores a version.

The model cites transcript lines by their number in the prompt (`chunking.refs`); andai maps each
number back to its segment id as soon as a reply arrives, and a number that names no line is kept
as written, so the writer drops it as an invalid id.

A transcript that fits one part is one call. A longer one is map-reduce: one call per part (packed
in order by `chunking.pack`, never truncated), then reduce calls over the partial minutes. A part
holds at most `part_tokens` estimated tokens of transcript, so that every call (map or reduce)
stays short: a call's time grows with what the model writes, and some routes cut a call off well
before the client's timeout. The reduce model writes only a summary and the groups of partial items
that say the same thing (`groups`, by reference); andai combines each group into one item (the first
item's text, the union of the group's evidence, the most confident owner and the first due phrase)
and keeps every other partial item as it was, so no grounded item is lost in the merge.
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
from andai_core.minutes.chunking import Segment, estimate_tokens, pack, refs
from andai_core.minutes.templates import Language, Template
from andai_core.text import fold

# Of the context left after the prompt and the reply, this share is kept free (estimates err).
SAFETY = 0.1
MIN_BUDGET = 256
# Estimated transcript tokens per call by default: about 15 s per call on the measured route.
PART_TOKENS = 5000


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


# ─── the reduce reply (groups of partial items that say the same thing) ───────────────────────────


class MergeGroup(_Draft):
    sources: list[str] = Field(
        description="References of two or more partial items that say the same thing, e.g. D1.2."
    )


class Merged(_Draft):
    summary: str
    groups: list[MergeGroup]


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
    part_tokens: int = PART_TOKENS,
) -> int:
    """Estimated tokens of transcript one call may carry: `part_tokens`, or less when the context
    left after the prompt and the reply is smaller."""
    fixed = estimate_tokens(prompt.overhead(template, language, attendees, meeting_date))
    fixed += estimate_tokens(json.dumps(strict_schema(Draft)))
    room = int((context_tokens - max_output_tokens - fixed) * (1 - SAFETY))
    if room < MIN_BUDGET:
        raise ValueError("the model's context is too small for minutes (ANDAI_LLM_CONTEXT_TOKENS)")
    return min(room, part_tokens)


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
    part_tokens: int = PART_TOKENS,
) -> Generation:
    """Draft minutes for a transcript. Raises `ValueError` for an empty transcript and the
    client's typed `LlmError`s when a call fails (nothing partial is returned)."""
    if not segments:
        raise ValueError("an empty transcript has no minutes")
    room = budget(
        template, language, attendees, meeting_date,
        context_tokens=context_tokens, max_output_tokens=max_output_tokens,
        part_tokens=part_tokens,
    )  # fmt: skip
    parts = pack(segments, room)
    numbers = refs(segments)
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
        drafts.append(_with_ids(Draft.model_validate(result.data), numbers))
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


# ─── map and reduce helpers ───────────────────────────────────────────────────────────────────────


def _with_ids(draft: Draft, numbers: dict[str, str]) -> Draft:
    """The draft with each cited line number replaced by its segment id."""

    def ids(evidence: Sequence[str]) -> list[str]:
        out: list[str] = []
        for cited in evidence:
            segment_id = numbers.get(cited.strip().strip("[]#").strip(), cited)
            if segment_id not in out:
                out.append(segment_id)
        return out

    for section, _ in SECTIONS:
        for item in getattr(draft, section):
            item.evidence = ids(item.evidence)
    return draft


def _referenced(part: int, draft: Draft) -> str:
    """A partial draft as the reduce prompt shows it: its summary, then one line per item that
    starts with the item's reference."""
    lines = [f"Part {part} summary: {draft.summary}"]
    for section, letter in SECTIONS:
        for i, item in enumerate(getattr(draft, section), start=1):
            text = " ".join((getattr(item, "title", None) or item.text).split())
            notes = []
            if section == "action_items":
                if item.owner:
                    notes.append(f"owner: {item.owner}")
                if item.due_text:
                    notes.append(f"due: {item.due_text}")
            lines.append(f"{letter}{part}.{i} {text}" + (f" ({'; '.join(notes)})" if notes else ""))
    return "\n".join(lines)


def _groups(drafts: list[Draft], room: int) -> list[list[Draft]]:
    """Consecutive drafts grouped so each group's reduce prompt fits the budget."""
    groups: list[list[Draft]] = []
    current: list[Draft] = []
    size = 0
    for d in drafts:
        cost = estimate_tokens(_referenced(99, d))
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


def _combine(section: str, items: list) -> BaseModel:
    """One item for a group: the first item's text, the union of the evidence; for an action item
    the owner the partial items name with the most confidence and the first due phrase."""
    combined = items[0].model_copy()
    combined.evidence = _union([i.evidence for i in items])
    if section == "action_items":
        named = [i for i in items if i.owner]
        if named:
            best = max(named, key=lambda i: i.owner_confidence or 0.0)
            combined.owner, combined.owner_confidence = best.owner, best.owner_confidence
        due = next((i for i in items if i.due_text), None)
        if due is not None:
            combined.due_text, combined.due = due.due_text, due.due
    return combined


def _resolve(merged: Merged, group: list[Draft]) -> Draft:
    """The group's partial drafts as one: each merge group of one section combined into one item
    where its first item stood, every other partial item kept in order, items with the same text
    combined, and the merged summary."""
    partial: dict[str, tuple[str, BaseModel]] = {
        f"{letter}{part}.{i}": (section, item)
        for section, letter in SECTIONS
        for part, d in enumerate(group, start=1)
        for i, item in enumerate(getattr(d, section), start=1)
    }
    lead: dict[str, list[str]] = {}  # the group's first reference → all its references
    taken: set[str] = set()
    for g in merged.groups:
        refs_ = [r.strip() for r in g.sources if r.strip() in partial and r.strip() not in taken]
        refs_ = list(dict.fromkeys(refs_))
        refs_ = [r for r in refs_ if partial[r][0] == partial[refs_[0]][0]] if refs_ else []
        if len(refs_) < 2:
            continue
        refs_.sort(key=list(partial).index)
        lead[refs_[0]] = refs_
        taken.update(refs_)
    sections: dict[str, list] = {section: [] for section, _ in SECTIONS}
    for ref, (section, item) in partial.items():
        if ref in lead:
            sections[section].append(_combine(section, [partial[r][1] for r in lead[ref]]))
        elif ref not in taken:
            sections[section].append(item)
    return Draft(
        summary=merged.summary,
        **{section: _dedupe(section, items) for section, items in sections.items()},
    )


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
    "MergeGroup",
    "Merged",
    "PART_TOKENS",
    "budget",
    "generate",
]
