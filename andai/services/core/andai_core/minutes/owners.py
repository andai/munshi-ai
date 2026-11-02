"""Who owns an action item (MOM-02, D-49).

The platform gives no speaker confidence, so the rule is: an action item keeps a named owner only
when the model's `ownerConfidence` is at least 0.6 **and** the name matches exactly one meeting
attendee; otherwise the owner is `unassigned`. Names are compared in their folded form
(`andai_core.text.fold`), so `प्रिया` matches `Priya` and case, diacritics and common spelling
variants do not matter. A part of a name matches when it picks out one attendee (`Priya` for
`Priya Sharma`); honorifics (`ji`, `sir`, `Mr` ...) are ignored; groups (`team`, `we`, `sab` ...)
are never owners. The owner kept is the attendee's name as the meeting lists it.

When the folded names match no attendee, they are compared once more on their consonant skeleton:
a leading vowel becomes one mark and every other vowel is dropped, so the vowels a transliteration
or a spelling adds or leaves out do not matter (`अय्यर`/`Iyer`, `Aravind`/`Arvind`). A word whose
skeleton is shorter than three letters keeps its folded form (`Ram` never meets `Rima`), and the
skeleton match too must pick out exactly one attendee.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping

from andai_core.text import fold

THRESHOLD = 0.6
UNASSIGNED = "unassigned"

_HONORIFICS = {
    fold(w)
    for w in (
        "ji", "जी", "sir", "madam", "maam", "ma'am", "mr", "mrs", "ms", "dr", "shri", "shrimati",
        "श्री", "श्रीमती", "smt", "bhai", "भाई", "sahab", "saab", "साहब", "didi", "दीदी",
    )
}  # fmt: skip
_GROUPS = {
    fold(w)
    for w in (
        "team", "the team", "we", "us", "our team", "all", "everyone", "everybody", "someone",
        "somebody", "anyone", "they", "them", "you", "unassigned", "unknown", "tbd", "none",
        "hum", "ham", "hum log", "hum sab", "sab", "sabhi", "sab log", "koi", "koi bhi", "aap",
        "aap log", "unknown speaker", "हम", "हम लोग", "हम सब", "सब", "सभी", "सब लोग", "टीम",
        "कोई", "आप", "आप लोग",
    )
}  # fmt: skip
_JOINT = {fold(w) for w in ("and", "aur", "और", "or", "ya", "या", "with", "saath")}
_LISTED = set(",&/+")
_VOWELS = re.compile(r"[aeiou]")
MIN_SKELETON = 3


def _tokens(name: str) -> tuple[str, ...]:
    return tuple(t for t in fold(name).split() if t not in _HONORIFICS)


def _skeleton(token: str) -> str:
    """A folded word's consonants: a leading vowel becomes `_`, every other vowel is dropped."""
    lead = "_" if token[:1] in "aeiou" else ""
    skeleton = lead + _VOWELS.sub("", token)
    return skeleton if len(skeleton) >= MIN_SKELETON else token


def _pick(
    wanted: tuple[str, ...],
    names: list[tuple[str, tuple[str, ...]]],
    key: Callable[[str], str],
) -> str | None:
    """The one attendee whose name matches `wanted` (whole, else in part) compared through `key`;
    `UNASSIGNED` when two attendees match equally; None when none does."""
    want = tuple(key(t) for t in wanted)
    keyed = [(name, tuple(key(t) for t in tokens)) for name, tokens in names if tokens]
    exact = {name for name, tokens in keyed if tokens == want}
    if len(exact) == 1:
        return exact.pop()
    if exact:
        return UNASSIGNED  # two attendees with the same name
    w = set(want)
    partial = {name for name, tokens in keyed if w <= set(tokens) or set(tokens) <= w}
    if len(partial) == 1:
        return partial.pop()
    return UNASSIGNED if partial else None


def _names(attendees: Iterable[Mapping | str]) -> list[str]:
    out: list[str] = []
    for a in attendees:
        name = a.get("name") if isinstance(a, Mapping) else a
        if isinstance(name, str) and name.strip() and name.strip() not in out:
            out.append(name.strip())
    return out


def resolve(owner: str | None, confidence: float | None, attendees: Iterable[Mapping | str]) -> str:
    """The attendee who owns the item, or `unassigned`."""
    if not owner or not owner.strip() or confidence is None or confidence < THRESHOLD:
        return UNASSIGNED
    if fold(owner) in _GROUPS:
        return UNASSIGNED
    wanted = _tokens(owner)
    if not wanted or " ".join(wanted) in _GROUPS or _JOINT & set(wanted) or _LISTED & set(owner):
        return UNASSIGNED  # a group, or several people
    names = [(name, _tokens(name)) for name in _names(attendees)]
    picked = _pick(wanted, names, str)
    if picked is None:
        picked = _pick(wanted, names, _skeleton)
    return picked or UNASSIGNED


__all__ = ["THRESHOLD", "UNASSIGNED", "resolve"]
