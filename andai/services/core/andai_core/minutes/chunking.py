"""Transcript lines for the prompt, and packing them into parts that fit the model's context.

Each segment becomes one line, `[n] mm:ss Speaker: text`, where `n` is the segment's number in the
meeting (1, 2, 3 ...) and the text is `text_raw` (the script as spoken, D-50). The model cites these
short numbers, which it copies far more reliably than 24-character segment ids; `refs` maps them
back to segment ids. Lines are packed in order into parts whose estimated size stays under a token
budget. Nothing is cut off: a segment too long for a part on its own is split into several lines
that all carry its number.

Token counts are estimated, not measured, and on the high side: Devanagari letters count half a
token each (tokenizers split Devanagari finely), everything else 0.3 of a token (about 3.3
characters per token).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

DEVANAGARI_TOKENS_PER_CHAR = 0.5
OTHER_TOKENS_PER_CHAR = 0.3


@dataclass(frozen=True)
class Segment:
    id: str
    start_s: float | None
    speaker: str
    text: str

    @classmethod
    def from_row(cls, row) -> Segment:
        """From a stored `transcript_segments` row: the raw text, as spoken."""
        return cls(id=row.id, start_s=row.start_s, speaker=row.speaker, text=row.text_raw)


def estimate_tokens(text: str) -> int:
    deva = sum(1 for c in text if "ऀ" <= c <= "ॿ")
    return math.ceil(deva * DEVANAGARI_TOKENS_PER_CHAR + (len(text) - deva) * OTHER_TOKENS_PER_CHAR)


def clock(start_s: float | None) -> str:
    if start_s is None or start_s < 0:
        return "--:--"
    whole = int(start_s)
    return f"{whole // 60:02d}:{whole % 60:02d}"


def refs(segments: Sequence[Segment]) -> dict[str, str]:
    """The number each segment carries in the prompt → its segment id."""
    return {str(n): s.id for n, s in enumerate(segments, start=1)}


def prefix(segment: Segment, ref: str) -> str:
    return f"[{ref}] {clock(segment.start_s)} {segment.speaker}: "


def line(segment: Segment, ref: str) -> str:
    return prefix(segment, ref) + " ".join(segment.text.split())


def _split(segment: Segment, ref: str, budget: int) -> list[str]:
    """A segment's text as several lines, each with the segment's number and within `budget`."""
    head = prefix(segment, ref)
    room = budget - estimate_tokens(head) - 1
    if room < 8:
        raise ValueError("the token budget is too small for one transcript line")
    lines: list[str] = []
    current = ""
    for word in segment.text.split():
        while estimate_tokens(word) > room:  # one "word" longer than a part: cut it by characters
            cut = len(word)
            while estimate_tokens(word[:cut]) > room:
                cut = max(1, cut * room // max(1, estimate_tokens(word[:cut])))
            if current:
                lines.append(head + current)
                current = ""
            lines.append(head + word[:cut])
            word = word[cut:]
        candidate = f"{current} {word}" if current else word
        if current and estimate_tokens(candidate) > room:
            lines.append(head + current)
            current = word
        else:
            current = candidate
    if current or not lines:
        lines.append(head + current)
    return lines


def pack(segments: Iterable[Segment], budget: int) -> list[list[str]]:
    """The transcript lines in order, numbered as `refs` numbers them, packed into parts of at most
    `budget` estimated tokens."""
    parts: list[list[str]] = []
    current: list[str] = []
    size = 0
    for n, segment in enumerate(segments, start=1):
        text = line(segment, str(n))
        cost = estimate_tokens(text) + 1
        pieces = [text] if cost <= budget else _split(segment, str(n), budget)
        for piece in pieces:
            cost = estimate_tokens(piece) + 1
            if current and size + cost > budget:
                parts.append(current)
                current, size = [], 0
            current.append(piece)
            size += cost
    if current:
        parts.append(current)
    return parts


def refs_in(lines: Sequence[str]) -> list[str]:
    """The segment numbers the lines carry, in order, each once."""
    seen: dict[str, None] = {}
    for text in lines:
        if text.startswith("[") and "]" in text:
            seen.setdefault(text[1 : text.index("]")], None)
    return list(seen)


__all__ = ["Segment", "clock", "estimate_tokens", "line", "pack", "refs", "refs_in"]
