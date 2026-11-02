"""Due dates said in English, Hindi or Hinglish, resolved against the meeting's local date
(MOM-02). Pure and deterministic: the same phrase and meeting date always give the same date, and
a phrase the rules do not cover gives no date.

The phrase is folded (`andai_core.text.fold`), so Devanagari and Roman spellings meet (`कल`,
`kal`). Rules, first match wins:

1. A written date: `2026-11-20`, `20/11`, `20-11-2026` (day first), `20 November`, `Nov 20th`,
   `20 नवंबर`. Without a year, the first such date on or after the meeting date.
2. A weekday (`Friday`, `shukravar`, `शुक्रवार`; `tak`, `by`, `this` change nothing): the first
   one after the meeting date. With `next` / `agle` / `अगले` before it, or with "next week" in the
   phrase, that weekday of the following calendar week (weeks start on Monday).
3. A count of days, weeks or months (`3 din mein`, `तीन दिन में`, `in two weeks`, `ek mahine`):
   the meeting date plus that many.
4. A day word: today / `aaj` / EOD / end of day / tonight → the meeting date; tomorrow / `kal` /
   `कल` → +1; day after tomorrow / `parso` / `परसों` → +2; `narso` / `नरसों` → +3.
5. Weeks and months: next week / `agle hafte` → Monday of the following week; end of next week
   → Friday of the following week; this week / end of the week / EOW / `hafte ke end tak` → the
   first Friday on or after the meeting date; next month / `agle mahine` → the 1st of the following
   month; end of the month / month end / EOM → the month's last day; end of next month → the
   following month's last day.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from andai_core.text import fold


def _folds(*words: str) -> frozenset[str]:
    return frozenset(fold(w) for w in words)


_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

_MONTHS: dict[str, int] = {}
for _number, _spellings in enumerate(
    (
        ("january", "jan", "janvari", "जनवरी"),
        ("february", "feb", "farvari", "फ़रवरी", "फरवरी"),
        ("march", "mar", "मार्च"),
        ("april", "apr", "aprail", "अप्रैल"),
        ("may", "mai", "मई"),
        ("june", "jun", "जून"),
        ("july", "jul", "julai", "जुलाई"),
        ("august", "aug", "agast", "अगस्त"),
        ("september", "sep", "sept", "sitambar", "सितंबर", "सितम्बर"),
        ("october", "oct", "aktubar", "oktobar", "अक्टूबर"),
        ("november", "nov", "navambar", "नवंबर", "नवम्बर"),
        ("december", "dec", "disambar", "दिसंबर", "दिसम्बर"),
    ),
    start=1,
):
    for _w in _spellings:
        _MONTHS[fold(_w)] = _number

_WEEKDAYS: dict[str, int] = {}
for _number, _spellings in enumerate(
    (
        ("monday", "somvar", "सोमवार"),
        ("tuesday", "mangalvar", "मंगलवार"),
        ("wednesday", "budhvar", "बुधवार"),
        ("thursday", "guruvar", "brihaspativar", "गुरुवार", "बृहस्पतिवार"),
        ("friday", "shukravar", "शुक्रवार"),
        ("saturday", "shanivar", "शनिवार"),
        ("sunday", "ravivar", "itvar", "रविवार", "इतवार"),
    )
):
    for _w in _spellings:
        _WEEKDAYS[fold(_w)] = _number

_NUMBERS: dict[str, int] = {}
for _number, _spellings in {
    1: ("one", "a", "an", "ek", "एक"),
    2: ("two", "do", "दो", "couple"),
    3: ("three", "teen", "तीन"),
    4: ("four", "char", "चार"),
    5: ("five", "paanch", "पाँच", "पांच"),
    6: ("six", "chhe", "chhah", "छह", "छः"),
    7: ("seven", "saat", "सात"),
    8: ("eight", "aath", "आठ"),
    9: ("nine", "nau", "नौ"),
    10: ("ten", "das", "दस"),
    15: ("fifteen", "pandrah", "पंद्रह"),
    20: ("twenty", "bees", "बीस"),
    30: ("thirty", "tees", "तीस"),
}.items():
    for _w in _spellings:
        _NUMBERS[fold(_w)] = _number

_DAY_UNITS = _folds("day", "days", "din", "दिन")
_WEEK_UNITS = _folds("week", "weeks", "hafte", "hafta", "हफ्ते", "हफ़्ते", "हफ्ता", "saptah", "सप्ताह")
_MONTH_UNITS = _folds("month", "months", "mahine", "mahina", "महीने", "महीना")
_NOT_A_DAY = _folds(
    "day",
    "days",
    "din",
    "दिन",
    "week",
    "weeks",
    "hafte",
    "हफ्ते",
    "month",
    "months",
    "mahine",
    "महीने",
    "baje",
    "बजे",
    "bje",
    "pm",
    "am",
    "hours",
    "hour",
    "ghante",
    "घंटे",
    "minute",
    "minutes",
    "min",
    "mins",
)
_NEXT = _folds("next", "agle", "agla", "अगले", "अगला")
_END = _folds("end", "ant", "अंत", "khatam")

_TODAY = (("today",), ("aaj",), ("eod",), ("tonight",), ("end", "of", "day"), ("aaj", "shaam"))
_TOMORROW = (("tomorrow",), ("tmrw",), ("kal",))
_DAY_AFTER = (("day", "after", "tomorrow"), ("parso",), ("parson",))
_THIRD_DAY = (("narso",), ("narson",))
_THIS_WEEK = (
    ("this", "week"),
    ("is", "hafte"),
    ("eow",),
    ("end", "of", "week"),
    ("end", "of", "the", "week"),
)
_END_OF_MONTH = (("eom",), ("month", "end"), ("end", "of", "month"), ("end", "of", "the", "month"))


def _phrases(*phrases: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(fold(w) for w in p) for p in phrases)


_TODAY_F, _TOMORROW_F, _DAY_AFTER_F, _THIRD_DAY_F, _THIS_WEEK_F, _END_OF_MONTH_F = (
    _phrases(*p) for p in (_TODAY, _TOMORROW, _DAY_AFTER, _THIRD_DAY, _THIS_WEEK, _END_OF_MONTH)
)

_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
# Day first. A dot needs the year ("20.11.2026"), so a time such as "5.30" is not a date.
_DMY = re.compile(r"\b(\d{1,2})([/-])(\d{1,2})(?:\2(\d{2}|\d{4}))?\b")
_DMY_DOTTED = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_ORDINAL = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?$")


def _has(tokens: list[str], phrase: tuple[str, ...]) -> bool:
    n = len(phrase)
    return any(tuple(tokens[i : i + n]) == phrase for i in range(len(tokens) - n + 1))


def _any(tokens: list[str], phrases: tuple[tuple[str, ...], ...]) -> bool:
    return any(_has(tokens, p) for p in phrases)


def _safe(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _next_on_or_after(meeting: date, month: int, day: int) -> date | None:
    for year in (meeting.year, meeting.year + 1):
        found = _safe(year, month, day)
        if found is not None and found >= meeting:
            return found
    return _safe(meeting.year + 1, month, day)


def _add_months(day: date, months: int) -> date:
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def _next_monday(day: date) -> date:
    return day + timedelta(days=7 - day.weekday())


def _written(raw: str, tokens: list[str], meeting: date) -> date | None:
    if m := _ISO.search(raw):
        return _safe(int(m[1]), int(m[2]), int(m[3]))
    if m := _DMY_DOTTED.search(raw):
        return _safe(int(m[3]), int(m[2]), int(m[1]))
    if m := _DMY.search(raw):
        day, month = int(m[1]), int(m[3])
        if m[4]:
            year = int(m[4]) + (2000 if len(m[4]) == 2 else 0)
            return _safe(year, month, day)
        return _next_on_or_after(meeting, month, day)
    for i, token in enumerate(tokens):
        if token not in _MONTHS:
            continue
        month = _MONTHS[token]
        for j in (i - 1, i + 1):  # "20 November" or "November 20th"
            if not 0 <= j < len(tokens) or not (d := _ORDINAL.match(tokens[j])):
                continue
            after = tokens[j + 1] if j + 1 < len(tokens) else ""
            if after in _NOT_A_DAY:  # "mai 2 din mein", "mai 5 baje": a count or a time
                continue
            k = j + 1 if j > i else i + 1  # a year right after the pair
            if k < len(tokens) and re.fullmatch(r"\d{4}", tokens[k]):
                return _safe(int(tokens[k]), month, int(d[1]))
            return _next_on_or_after(meeting, month, int(d[1]))
    return None


def _weekday(tokens: list[str], meeting: date) -> date | None:
    next_week = any(
        tokens[i] in _NEXT and tokens[i + 1] in _WEEK_UNITS for i in range(len(tokens) - 1)
    )
    for i, token in enumerate(tokens):
        if token not in _WEEKDAYS:
            continue
        target = _WEEKDAYS[token]
        if next_week or (i > 0 and tokens[i - 1] in _NEXT):
            return _next_monday(meeting) + timedelta(days=target)
        ahead = (target - meeting.weekday()) % 7 or 7
        return meeting + timedelta(days=ahead)
    return None


def _count(tokens: list[str], meeting: date) -> date | None:
    for i in range(len(tokens) - 1):
        word, unit = tokens[i], tokens[i + 1]
        n = int(word) if word.isdigit() else _NUMBERS.get(word)
        if n is None or n <= 0 or n > 366:
            continue
        if unit in _DAY_UNITS:
            return meeting + timedelta(days=n)
        if unit in _WEEK_UNITS:
            return meeting + timedelta(weeks=n)
        if unit in _MONTH_UNITS:
            return _add_months(meeting, n)
    return None


def _relative(tokens: list[str], meeting: date) -> date | None:
    if _any(tokens, _DAY_AFTER_F):  # before "tomorrow": "day after tomorrow" contains it
        return meeting + timedelta(days=2)
    if _any(tokens, _THIRD_DAY_F):
        return meeting + timedelta(days=3)
    if _any(tokens, _TOMORROW_F):
        return meeting + timedelta(days=1)
    if _any(tokens, _TODAY_F):
        return meeting
    pairs = list(zip(tokens, tokens[1:], strict=False))
    next_week = any(a in _NEXT and b in _WEEK_UNITS for a, b in pairs)
    next_month = any(a in _NEXT and b in _MONTH_UNITS for a, b in pairs)
    ending = bool(_END & set(tokens))
    if next_week:
        monday = _next_monday(meeting)
        return monday + timedelta(days=4) if ending else monday
    if next_month:
        first = _add_months(meeting.replace(day=1), 1)
        return _month_end(first) if ending else first
    if _any(tokens, _END_OF_MONTH_F) or (ending and _MONTH_UNITS & set(tokens)):
        return _month_end(meeting)
    if _any(tokens, _THIS_WEEK_F) or (ending and _WEEK_UNITS & set(tokens)):
        return meeting + timedelta(days=(4 - meeting.weekday()) % 7)
    return None


def resolve(due_text: str | None, meeting_date: date) -> date | None:
    """The date `due_text` names, counted from `meeting_date`; None when no rule covers it."""
    if not due_text or not due_text.strip():
        return None
    raw = due_text.translate(_DEVANAGARI_DIGITS)
    tokens = fold(raw).split()
    for rule in (
        lambda: _written(raw, tokens, meeting_date),
        lambda: _weekday(tokens, meeting_date),
        lambda: _count(tokens, meeting_date),
        lambda: _relative(tokens, meeting_date),
    ):
        found = rule()
        if found is not None:
            return found
    return None


__all__ = ["resolve"]
