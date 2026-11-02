"""Transcript lines for the prompt, and packing them into parts that fit the model's context.

Each segment becomes one line, `[seg_id] mm:ss Speaker: text`, where the text is `text_raw` (the
script as spoken, D-50). Lines are packed in order into parts whose estimated size stays under a
token budget. Nothing is cut off: a segment too long for a part on its own is split into several
lines that all carry its id.

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


def prefix(segment: Segment) -> str:
    return f"[{segment.id}] {clock(segment.start_s)} {segment.speaker}: "


def line(segment: Segment) -> str:
    return prefix(segment) + " ".join(segment.text.split())


def _split(segment: Segment, budget: int) -> list[str]:
    """A segment's text as several lines, each with the segment's id and within `budget`."""
    head = prefix(segment)
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
    """The transcript lines in order, packed into parts of at most `budget` estimated tokens."""
    parts: list[list[str]] = []
    current: list[str] = []
    size = 0
    for segment in segments:
        text = line(segment)
        cost = estimate_tokens(text) + 1
        pieces = [text] if cost <= budget else _split(segment, budget)
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


def ids_in(lines: Sequence[str]) -> list[str]:
    """The segment ids the lines carry, in order, each once."""
    seen: dict[str, None] = {}
    for text in lines:
        if text.startswith("[") and "]" in text:
            seen.setdefault(text[1 : text.index("]")], None)
    return list(seen)


__all__ = ["Segment", "clock", "estimate_tokens", "ids_in", "line", "pack"]
