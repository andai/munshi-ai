"""Normaliser v0: Devanagari → Roman Hinglish (`canonical`), the script tag (`script_of`) and the
search fold (`fold`). Pure functions over fixed word tables."""

from __future__ import annotations

import pytest

from andai_core.text import canonical, fold, script_of

# ─── canonical: Devanagari word table ─────────────────────────────────────────────────────────────

WORDS: list[tuple[str, str, str]] = [
    # (devanagari, roman, what the case covers)
    ("कल", "kal", "word-final schwa"),
    ("क्या", "kya", "virama conjunct"),
    ("है", "hai", "ai matra"),
    ("हैं", "hain", "anusvara at word end"),
    ("मैं", "main", "ai matra with anusvara"),
    ("नहीं", "nahin", "long i shortened before a final nasal"),
    ("कहीं", "kahin", "long i shortened before a final nasal"),
    ("हम", "ham", "two consonants"),
    ("तुम", "tum", "u matra"),
    ("आप", "aap", "independent aa"),
    ("आज", "aaj", "independent aa"),
    ("और", "aur", "independent au"),
    ("एक", "ek", "independent e"),
    ("का", "ka", "final aa shortened"),
    ("की", "ki", "final ii shortened"),
    ("को", "ko", "o matra"),
    ("से", "se", "e matra"),
    ("पर", "par", "final schwa"),
    ("भी", "bhi", "aspirate"),
    ("तो", "to", "o matra"),
    ("था", "tha", "aspirate dental"),
    ("थे", "the", "aspirate dental"),
    ("गया", "gaya", "y with final aa"),
    ("किया", "kiya", "i matra"),
    ("होगा", "hoga", "medial o"),
    ("करना", "karna", "medial schwa in C-V-C-a-C-V"),
    ("करेंगे", "karenge", "anusvara before g"),
    ("सकते", "sakte", "medial schwa"),
    ("सकता", "sakta", "medial schwa"),
    ("समझना", "samajhna", "medial schwa, right to left"),
    ("समझ", "samajh", "final schwa only"),
    ("कमल", "kamal", "no medial deletion before a final consonant"),
    ("कमला", "kamla", "medial schwa"),
    ("बदलना", "badalna", "right to left keeps the first a"),
    ("निकलना", "nikalna", "right to left"),
    ("मतलब", "matlab", "medial and final schwa"),
    ("सबसे", "sabse", "medial schwa"),
    ("पहले", "pahle", "medial schwa before e"),
    ("रहना", "rahna", "medial schwa"),
    ("कहा", "kaha", "final aa shortened"),
    ("बहुत", "bahut", "u matra then final schwa"),
    ("समय", "samay", "final schwa after y"),
    ("उसका", "uska", "medial schwa after an independent vowel"),
    ("अपना", "apna", "medial schwa after an independent vowel"),
    ("मिलकर", "milkar", "medial schwa after i"),
    ("काम", "kaam", "medial aa"),
    ("बात", "baat", "medial aa"),
    ("दिन", "din", "i matra"),
    ("ठीक", "theek", "medial long i, retroflex aspirate"),
    ("मीटिंग", "meeting", "anusvara before g, loanword"),
    ("हिंदी", "hindi", "anusvara before d"),
    ("संपर्क", "sampark", "anusvara before p is m; final cluster drops schwa"),
    ("अंदर", "andar", "independent a with anusvara"),
    ("संभलना", "sambhalna", "anusvara before bh"),
    ("हँसना", "hansna", "chandrabindu, medial schwa after a nasal vowel"),
    ("हूँ", "hoon", "long u kept before chandrabindu"),
    ("हाँ", "haan", "long a kept before chandrabindu"),
    ("दूँगा", "doonga", "chandrabindu medial"),
    ("पहुँच", "pahunch", "chandrabindu after u"),
    ("दुःख", "duhkh", "visarga"),
    ("क्यों", "kyon", "conjunct with o and anusvara"),
    ("ज़रूर", "zaroor", "nukta z"),
    ("ज़रूरी", "zaroori", "nukta z, final ii"),
    ("फ़ोन", "fon", "nukta f"),
    ("फिर", "phir", "plain ph"),
    ("बड़ा", "bada", "nukta d"),
    ("ख़त्म", "khatm", "nukta kh; cluster keeps the schwa before it"),
    ("अंग्रेज़ी", "angrezi", "conjunct with r, nukta z"),
    ("कृपया", "kripya", "vocalic r, medial schwa"),
    ("ग्राहक", "graahak", "initial r-conjunct"),
    ("मित्र", "mitra", "final schwa kept after a conjunct ending in r"),
    ("राज्य", "raajya", "final schwa kept after a conjunct ending in y"),
    ("धर्म", "dharm", "final schwa dropped after a conjunct ending in m"),
    ("अच्छा", "acchha", "geminate c-chh"),
    ("सच्चा", "saccha", "geminate c-c"),
    ("पक्का", "pakka", "geminate k-k"),
    ("बुद्धि", "buddhi", "geminate d-dh"),
    ("चाहिए", "chaahiye", "y glide before e after i"),
    ("लिए", "liye", "y glide"),
    ("गए", "gaye", "y glide after a"),
    ("हुए", "hue", "no glide after u"),
    ("ऑफ़िस", "ofis", "candra o, nukta f"),
    ("में", "mein", "documented exception"),
    ("न", "na", "single akshara keeps its a"),
    ("जगत्", "jagat", "explicit virama at word end"),
]


@pytest.mark.parametrize(("deva", "roman", "case"), WORDS, ids=[w[1] for w in WORDS])
def test_word_table(deva: str, roman: str, case: str) -> None:
    assert canonical(deva) == roman, case


def test_the_word_table_is_large_enough() -> None:
    assert len(WORDS) >= 60


@pytest.mark.parametrize(
    ("text", "roman"),
    [
        ("०१२३४५६७८९", "0123456789"),
        ("२०२६", "2026"),
        ("ठीक है।", "theek hai."),
        ("हाँ॥", "haan."),
        ("कल की मीटिंग में deadline तय हुई", "kal ki meeting mein deadline tay hui"),
        ("क्या हम Friday तक ship कर सकते हैं", "kya ham Friday tak ship kar sakte hain"),
        ("ठीक है, मैं report भेज दूँगा", "theek hai, main report bhej doonga"),
        ("मीटिंग 3:30 बजे?", "meeting 3:30 baje?"),
        ("(नहीं!)", "(nahin!)"),
        ("ok—ठीक", "ok—theek"),
    ],
)
def test_lines_with_digits_punctuation_and_latin(text: str, roman: str) -> None:
    assert canonical(text) == roman


def test_decomposed_and_precomposed_nukta_give_the_same_roman() -> None:
    precomposed = "ज़रूर"  # ज़ as one code point
    decomposed = "ज़रूर"  # ज + nukta
    assert canonical(precomposed) == canonical(decomposed) == "zaroor"


def test_zero_width_joiners_inside_a_word_are_dropped() -> None:
    assert canonical("क्‍या") == "kya"


@pytest.mark.parametrize(
    "text",
    [
        "Let's ship it by Friday.",
        "café naïve résumé",
        "  spaced   out \t text\n",
        "Ünïcödé — “quotes” ‘and’ dashes…",
        "zaroor kyaa nahīn",
        "",
        "١٢٣ مرحبا",
        "a‍b",
    ],
)
def test_latin_and_other_text_passes_through_untouched(text: str) -> None:
    assert canonical(text) == text


@pytest.mark.parametrize(
    "text",
    [w[0] for w in WORDS]
    + ["कल की मीटिंग में deadline तय हुई", "ठीक है, मैं report भेज दूँगा", "Friday", ""],
)
def test_canonical_is_idempotent(text: str) -> None:
    once = canonical(text)
    assert canonical(once) == once


def test_canonical_output_is_ascii_for_devanagari_input() -> None:
    for deva, _, _ in WORDS:
        assert canonical(deva).isascii(), deva


def test_the_tables_are_deterministic() -> None:
    lines = [w[0] for w in WORDS] + ["कल की मीटिंग में deadline तय हुई"]
    first = "\n".join(canonical(x) + "\t" + fold(x) + "\t" + script_of(x) for x in lines)
    second = "\n".join(canonical(x) + "\t" + fold(x) + "\t" + script_of(x) for x in lines)
    assert first.encode() == second.encode()


# ─── script_of ────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "tag"),
    [
        ("hello world", "latn"),
        ("café", "latn"),
        ("कल की मीटिंग", "deva"),
        ("ठीक है।", "deva"),
        ("कल की मीटिंग में deadline", "mixed"),
        ("مرحبا", "other"),
        ("مرحبا hello", "mixed"),
        ("12:30 !", "other"),
        ("", "other"),
        ("२०२६", "other"),
    ],
)
def test_script_of(text: str, tag: str) -> None:
    assert script_of(text) == tag


# ─── fold ─────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "variants",
    [
        ["kya", "kyaa", "Kyaa", "क्या"],
        ["nahi", "nahin", "nahīn", "naheen", "नहीं"],
        ["zaroor", "jaroor", "zarur", "jarur", "ज़रूर", "जरूर"],
        ["phir", "fir", "फिर"],
        ["wala", "vala", "waala", "वाला"],
        ["accha", "acha", "achha", "achchha", "अच्छा"],
        ["kal", "कल"],
        ["meeting", "मीटिंग"],
        ["hain", "hai", "है", "हैं"],
        ["qissa", "kissa"],
        ["haan", "han", "हाँ"],
    ],
)
def test_fold_meets_spelling_variants(variants: list[str]) -> None:
    forms = {fold(v) for v in variants}
    assert len(forms) == 1, dict(zip(variants, [fold(v) for v in variants], strict=True))


@pytest.mark.parametrize(
    ("a", "b"),
    [("kal", "kam"), ("kya", "kyon"), ("ship", "sip"), ("deadline", "dateline")],
)
def test_fold_keeps_different_words_apart(a: str, b: str) -> None:
    assert fold(a) != fold(b)


def test_fold_of_a_line_is_words_separated_by_single_spaces() -> None:
    assert fold("कल की मीटिंग में  Deadline, तय हुई!") == "kal ki miting mei deadline tay hui"


def test_fold_is_idempotent() -> None:
    for text in [w[0] for w in WORDS] + ["Kyaa zaroor? Phir milte hain."]:
        once = fold(text)
        assert fold(once) == once, text
