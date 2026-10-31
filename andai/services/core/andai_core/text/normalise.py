"""Normaliser v0: Devanagari → Roman Hinglish, the script tag, and the search fold.

Pure and deterministic: one mapping table plus the schwa rules, no network, no model, no randomness.
The rules and the v0 limits are listed in this package's README.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

ScriptTag = Literal["latn", "deva", "mixed", "other"]

# ─── the mapping table ────────────────────────────────────────────────────────────────────────────

NUKTA = "़"
VIRAMA = "्"
ANUSVARA = "ं"
CHANDRABINDU = "ँ"
VISARGA = "ः"

# Consonants. Nukta forms map to the one plain letter Hinglish writers use for them.
CONSONANTS: dict[str, str] = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "व": "v",
    "श": "sh", "ष": "sh", "स": "s", "ह": "h",
    "ळ": "l", "ऩ": "n", "ऱ": "r", "ऴ": "l",
}  # fmt: skip
NUKTA_CONSONANTS: dict[str, str] = {
    "क": "k", "ख": "kh", "ग": "g", "ज": "z", "ड": "d", "ढ": "dh", "फ": "f", "य": "y",
}  # fmt: skip
# The precomposed nukta letters are composition exclusions: spell them as base + nukta.
PRECOMPOSED_NUKTA: dict[str, str] = {
    "क़": "क", "ख़": "ख", "ग़": "ग", "ज़": "ज",
    "ड़": "ड", "ढ़": "ढ", "फ़": "फ", "य़": "य",
}  # fmt: skip

# Vowels, as an independent letter and as a matra. "aa", "ii", "uu" are the long vowels whose
# spelling depends on position (see `_vowel_roman`).
INDEPENDENT_VOWELS: dict[str, str] = {
    "अ": "a", "आ": "aa", "इ": "i", "ई": "ii", "उ": "u", "ऊ": "uu", "ऋ": "ri", "ॠ": "ri",
    "ऌ": "li", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऍ": "e", "ऑ": "o", "ऎ": "e", "ऒ": "o",
    "ॲ": "e",
}  # fmt: skip
MATRAS: dict[str, str] = {
    "ा": "aa", "ि": "i", "ी": "ii", "ु": "u", "ू": "uu",
    "ृ": "ri", "ॄ": "ri", "ॢ": "li", "ॣ": "li",
    "े": "e", "ै": "ai", "ो": "o", "ौ": "au",
    "ॅ": "e", "ॉ": "o", "ॆ": "e", "ॊ": "o",
    "ॕ": "e", "ॖ": "u", "ॗ": "uu",
}  # fmt: skip
LONG = {"aa": ("aa", "a"), "ii": ("ee", "i"), "uu": ("oo", "u")}  # (medial, word-final)

DIGITS = {chr(0x0966 + i): str(i) for i in range(10)}
PUNCTUATION = {"।": ".", "॥": ".", "॰": "."}

# Unaspirated stop → its aspirated pair, for geminates (च्छ → cchh, द्ध → ddh).
ASPIRATE = {
    "क": "ख", "ग": "घ", "च": "छ", "ज": "झ", "ट": "ठ", "ड": "ढ", "त": "थ", "द": "ध", "प": "फ",
    "ब": "भ",
}  # fmt: skip
LABIALS = {"प", "फ", "ब", "भ", "म"}
KEEPS_FINAL_SCHWA = {"य", "र", "व"}  # a word-final conjunct ending in one of these keeps its `a`

# Whole words whose rule output is not the spelling Hinglish writers use. Keep this list short.
EXCEPTIONS: dict[str, str] = {"में": "mein"}

IGNORED = {"‌", "‍", "ऽ", "॑", "॒", "॓", "॔", "ॱ"}

_DEVA_WORD = re.compile(r"[ऀ-ॣॲ-ॿ][ऀ-ॣॱ-ॿ‌‍]*")
_DEVA_OTHER = re.compile(r"[।-॰]")


# ─── canonical ────────────────────────────────────────────────────────────────────────────────────


@dataclass
class _Akshara:
    """One syllable: a consonant cluster (empty for an independent vowel), its vowel and a coda."""

    consonants: list[tuple[str, bool]] = field(default_factory=list)  # (letter, has nukta)
    vowel: str | None = None  # roman key; "a" = the inherent vowel; None = explicit virama
    inherent: bool = False
    independent: bool = False
    coda: list[str] = field(default_factory=list)  # anusvara / chandrabindu / visarga, in order
    dropped: bool = False  # inherent vowel removed by the schwa rules


def _parse(word: str) -> list[_Akshara]:
    chars = [PRECOMPOSED_NUKTA.get(c, c) + (NUKTA if c in PRECOMPOSED_NUKTA else "") for c in word]
    flat = [c for c in "".join(chars) if c not in IGNORED]
    out: list[_Akshara] = []
    current: _Akshara | None = None
    pending_cluster: list[tuple[str, bool]] = []
    i = 0
    while i < len(flat):
        c = flat[i]
        if c in CONSONANTS:
            nukta = i + 1 < len(flat) and flat[i + 1] == NUKTA
            i += 2 if nukta else 1
            pending_cluster.append((c, nukta))
            if i < len(flat) and flat[i] == VIRAMA:
                i += 1
                if i < len(flat) and flat[i] in CONSONANTS:
                    continue  # conjunct: the next consonant joins this cluster
                current = _Akshara(consonants=pending_cluster, vowel=None)
                out.append(current)
                pending_cluster = []
                continue
            if i < len(flat) and flat[i] in MATRAS:
                current = _Akshara(consonants=pending_cluster, vowel=MATRAS[flat[i]])
                i += 1
            else:
                current = _Akshara(consonants=pending_cluster, vowel="a", inherent=True)
            out.append(current)
            pending_cluster = []
            continue
        if c in INDEPENDENT_VOWELS:
            current = _Akshara(vowel=INDEPENDENT_VOWELS[c], independent=True)
            out.append(current)
        elif c in (ANUSVARA, CHANDRABINDU, VISARGA):
            if current is None:
                current = _Akshara(vowel="a", independent=True)
                out.append(current)
            current.coda.append(c)
        elif c == "ॐ":
            current = _Akshara(vowel="om", independent=True)
            out.append(current)
        # anything else (a stray nukta, virama or matra) carries no sound of its own
        i += 1
    if pending_cluster:
        out.append(_Akshara(consonants=pending_cluster, vowel=None))
    return out


def _apply_schwa_rules(syllables: list[_Akshara]) -> None:
    last = syllables[-1]
    if len(syllables) > 1 and last.inherent and not last.coda:
        cluster_end = last.consonants[-1][0] if last.consonants else ""
        if not (len(last.consonants) > 1 and cluster_end in KEEPS_FINAL_SCHWA):
            last.dropped = True
    # Medial: V C a C V → V C C V, scanned right to left so each deletion sees the one after it.
    for k in range(len(syllables) - 2, 0, -1):
        s = syllables[k]
        if not s.inherent or s.dropped or s.coda or len(s.consonants) != 1:
            continue
        prev, nxt = syllables[k - 1], syllables[k + 1]
        prev_voiced = prev.vowel is not None and not prev.dropped
        next_cv = bool(nxt.consonants) and nxt.vowel is not None and not nxt.dropped
        if prev_voiced and next_cv:
            s.dropped = True


def _consonant_roman(letter: str, nukta: bool) -> str:
    if nukta and letter in NUKTA_CONSONANTS:
        return NUKTA_CONSONANTS[letter]
    return CONSONANTS[letter]


def _cluster_roman(cluster: list[tuple[str, bool]]) -> str:
    parts: list[str] = []
    for j, (letter, nukta) in enumerate(cluster):
        roman = _consonant_roman(letter, nukta)
        if j + 1 < len(cluster) and not nukta:
            following = cluster[j + 1][0]
            if following == letter or following == ASPIRATE.get(letter):
                roman = roman[0]  # geminate: च्च → cch, च्छ → cchh
        parts.append(roman)
    return "".join(parts)


def _first_consonant(syllables: list[_Akshara], k: int) -> str | None:
    if k + 1 < len(syllables) and syllables[k + 1].consonants:
        return syllables[k + 1].consonants[0][0]
    return None


def _vowel_roman(syllables: list[_Akshara], k: int) -> str:
    s = syllables[k]
    if s.vowel is None or s.dropped:
        return ""
    vowel = s.vowel
    if vowel in LONG:
        medial, final = LONG[vowel]
        is_last = k == len(syllables) - 1
        lone_vowel = len(syllables) == 1 and not s.consonants
        nasal_only = all(c in (ANUSVARA, CHANDRABINDU) for c in s.coda)
        shorten = is_last and not lone_vowel and (not s.coda or (vowel == "ii" and nasal_only))
        vowel = final if shorten else medial
    if s.independent and vowel in ("e", "ai") and k > 0:
        before = syllables[k - 1]
        if before.vowel in ("a", "aa", "i", "ii") and not before.dropped and not before.coda:
            vowel = "y" + vowel  # glide: लिए → liye, गए → gaye
    return vowel


def _coda_roman(syllables: list[_Akshara], k: int) -> str:
    out = []
    for c in syllables[k].coda:
        if c == VISARGA:
            out.append("h")
        elif c == ANUSVARA and _first_consonant(syllables, k) in LABIALS:
            out.append("m")
        else:
            out.append("n")
    return "".join(out)


def _word(word: str) -> str:
    if word in EXCEPTIONS:
        return EXCEPTIONS[word]
    syllables = _parse(word)
    if not syllables:
        return ""
    _apply_schwa_rules(syllables)
    return "".join(
        _cluster_roman(s.consonants) + _vowel_roman(syllables, k) + _coda_roman(syllables, k)
        for k, s in enumerate(syllables)
    )


def canonical(text: str) -> str:
    """Roman Hinglish for `text`: Devanagari runs transliterated, everything else left as it is."""
    text = _DEVA_WORD.sub(lambda m: _word(m.group(0)), text)
    return _DEVA_OTHER.sub(lambda m: DIGITS.get(m.group(0)) or PUNCTUATION[m.group(0)], text)


# ─── script_of ────────────────────────────────────────────────────────────────────────────────────


def _is_latin(c: str) -> bool:
    o = ord(c)
    return (
        ("a" <= c <= "z")
        or ("A" <= c <= "Z")
        or (0x00C0 <= o <= 0x024F and o not in (0xD7, 0xF7))
        or 0x1E00 <= o <= 0x1EFF
        or 0x2C60 <= o <= 0x2C7F
        or 0xA720 <= o <= 0xA7FF
        or 0xFF21 <= o <= 0xFF3A
        or 0xFF41 <= o <= 0xFF5A
    )


def _is_devanagari(c: str) -> bool:
    o = ord(c)
    in_block = 0x0900 <= o <= 0x097F or 0xA8E0 <= o <= 0xA8FF or 0x1CD0 <= o <= 0x1CFF
    return in_block and not (0x0964 <= o <= 0x0970)  # dandas, digits, abbreviation sign


def script_of(text: str) -> ScriptTag:
    """`latn`, `deva`, `mixed` (letters of more than one script) or `other` (no Latin or
    Devanagari letters, or no letters at all)."""
    seen: set[str] = set()
    for c in text:
        if _is_devanagari(c):
            seen.add("deva")
        elif _is_latin(c):
            seen.add("latn")
        elif c.isalpha():
            seen.add("other")
        if len(seen) > 1:
            return "mixed"
    if len(seen) == 1:
        tag = seen.pop()
        if tag in ("latn", "deva"):
            return tag  # type: ignore[return-value]
    return "other"


# ─── fold ─────────────────────────────────────────────────────────────────────────────────────────

_FOLD_STEPS: tuple[tuple[str, str], ...] = (
    ("ph", "f"),
    ("w", "v"),
    ("z", "j"),
    ("q", "k"),
    ("chh", "ch"),
    ("chch", "ch"),
    ("ee", "i"),
    ("ii", "i"),
    ("oo", "u"),
    ("uu", "u"),
)
_REPEAT = re.compile(r"([a-z])\1+")
_FINAL_NASAL = re.compile(r"(?<=[aeiou])n$")
_WORD = re.compile(r"\w+")


def _fold_word(word: str) -> str:
    while True:
        before = word
        for old, new in _FOLD_STEPS:
            word = word.replace(old, new)
        word = _REPEAT.sub(r"\1", word)
        if len(word) >= 3:
            word = _FINAL_NASAL.sub("", word)
        if word == before:
            return word


def fold(text: str) -> str:
    """The search form of `text`: canonical Roman, lowercased, diacritics stripped, spelling
    variants merged, words separated by single spaces. Queries and documents use the same fold."""
    roman = unicodedata.normalize("NFKD", canonical(text))
    roman = "".join(c for c in roman if not unicodedata.combining(c)).casefold()
    return " ".join(_fold_word(w) for w in _WORD.findall(roman) if w.strip("_"))
