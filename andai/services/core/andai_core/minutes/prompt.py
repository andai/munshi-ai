"""The messages the model gets: one system message with the rules, one user message with the
meeting (date, attendees, transcript lines or partial minutes).

The rules ask for grounded output only: every item cites the ids of the segments that support it,
what was not said is left out, an owner is named only when the transcript names one (with the
model's confidence), and a due date is quoted as said. andai, not the model, decides the owner and
resolves the date (`owners.py`, `dates.py`).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date

from andai_core.minutes.templates import LANGUAGE_INSTRUCTION, Language, Template

RULES = """You write the minutes of a meeting from its transcript.
Transcript lines look like `[segment id] mm:ss Speaker: what was said`. Speech may be English, \
Hindi (Devanagari) or Hinglish.

Rules:
1. Use only what was said in the transcript. Leave out anything that was not said: an empty list \
is the right answer when nothing of that kind was said. Never invent people, numbers or dates.
2. Every decision, action item, topic and risk lists in `evidence` the ids of the segments that \
support it, copied exactly from the brackets: at least one id, only ids that appear in the \
transcript.
3. `owner` is the person who will do an action item, only when the transcript clearly names them \
(write the name as said); otherwise null. `ownerConfidence` (0 to 1) is how sure you are that the \
transcript names this person as the owner. Groups ("the team", "we", "everyone") are not owners.
4. `dueText` is the due phrase exactly as said (for example "kal tak", "next Friday", \
"31 October"), or null when no due time was said. `due` is that date as YYYY-MM-DD only when you \
are sure of it from the meeting date; otherwise null.
5. {language}
6. {guidance}"""

PART_NOTE = (
    "This is part {index} of {count} of the transcript, in order. Write minutes for this part only; "
    "they will be merged with the other parts."
)

REDUCE_RULES = """You merge partial minutes of one meeting into its final minutes.
Each partial minutes was written from one consecutive part of the transcript. Every partial item \
has a reference such as `D1.2` (decision 2 of part 1); A = action item, T = topic, R = risk.

Rules:
1. Merge items that say the same thing into one; keep every distinct item. Do not add anything \
that is not in the partial minutes.
2. Every merged item lists in `sources` the references of all the partial items it comes from \
(at least one, exactly as written).
3. Write one summary for the whole meeting from the partial summaries.
4. For action items keep `owner`, `ownerConfidence`, `dueText` and `due` from the partial items \
(when they disagree, keep the most specific one that the partial items state).
5. {language}
6. {guidance}"""


def _header(attendees: Sequence[str], meeting_date: date) -> str:
    people = ", ".join(attendees) if attendees else "(not known)"
    return f"Meeting date: {meeting_date.isoformat()} ({meeting_date:%A})\nAttendees: {people}"


def system_message(template: Template, language: Language) -> dict:
    return {
        "role": "system",
        "content": RULES.format(
            language=LANGUAGE_INSTRUCTION[language], guidance=template.guidance
        ),
    }


def messages(
    lines: Sequence[str],
    *,
    attendees: Sequence[str],
    template: Template,
    language: Language,
    meeting_date: date,
    part: tuple[int, int] | None = None,
) -> list[dict]:
    """The map (or single) call: rules, then the meeting and its transcript lines."""
    parts = [_header(attendees, meeting_date)]
    if part is not None and part[1] > 1:
        parts.append(PART_NOTE.format(index=part[0], count=part[1]))
    parts.append("Transcript:\n" + "\n".join(lines))
    return [system_message(template, language), {"role": "user", "content": "\n\n".join(parts)}]


def reduce_messages(
    partials: Sequence[dict],
    *,
    attendees: Sequence[str],
    template: Template,
    language: Language,
    meeting_date: date,
) -> list[dict]:
    """The reduce call: rules, then the meeting and the referenced partial minutes as JSON."""
    system = {
        "role": "system",
        "content": REDUCE_RULES.format(
            language=LANGUAGE_INSTRUCTION[language], guidance=template.guidance
        ),
    }
    body = json.dumps(list(partials), ensure_ascii=False, indent=1)
    user = f"{_header(attendees, meeting_date)}\n\nPartial minutes:\n{body}"
    return [system, {"role": "user", "content": user}]


def overhead(
    template: Template, language: Language, attendees: Sequence[str], meeting_date: date
) -> str:
    """The text every map prompt carries besides its transcript lines (for the token budget)."""
    return (
        system_message(template, language)["content"]
        + _header(attendees, meeting_date)
        + PART_NOTE.format(index=999, count=999)
        + "Transcript:\n"
    )


__all__ = ["messages", "overhead", "reduce_messages", "system_message"]
