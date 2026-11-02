"""Who owns an action item (MOM-02, D-49).

The platform gives no speaker confidence, so the rule is: an action item keeps a named owner only
when the model's `ownerConfidence` is at least 0.6 **and** the name matches exactly one meeting
attendee; otherwise the owner is `unassigned`. Names are compared in their folded form
(`andai_core.text.fold`), so `प्रिया` matches `Priya` and case, diacritics and common spelling
variants do not matter. A part of a name matches when it picks out one attendee (`Priya` for
`Priya Sharma`); honorifics (`ji`, `sir`, `Mr` ...) are ignored; groups (`team`, `we`, `sab` ...)
are never owners. The owner kept is the attendee's name as the meeting lists it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

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


def _tokens(name: str) -> tuple[str, ...]:
    return tuple(t for t in fold(name).split() if t not in _HONORIFICS)


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
    exact = {name for name, tokens in names if tokens and tokens == wanted}
    if len(exact) == 1:
        return exact.pop()
    if exact:
        return UNASSIGNED  # two attendees with the same name
    want = set(wanted)
    partial = {
        name for name, tokens in names if tokens and (want <= set(tokens) or set(tokens) <= want)
    }
    return partial.pop() if len(partial) == 1 else UNASSIGNED


__all__ = ["THRESHOLD", "UNASSIGNED", "resolve"]
