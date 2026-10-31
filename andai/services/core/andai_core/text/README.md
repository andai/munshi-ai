# andai/services/core/andai_core/text

Normaliser v0: the one module that transcript display, search indexing and MoM input use to put
Hindi and Hinglish text into one script. `normalise.py` is pure and deterministic: a mapping table and
a few rules, with no network, no model and no randomness. The same input gives the same output on
every run and every machine.

- `canonical(text)` turns each run of Devanagari into Roman Hinglish (`कल` → `kal`, `क्या` → `kya`,
  `है` → `hai`). Latin text, other scripts, spacing and punctuation are left exactly as they are.
  Devanagari digits become `0-9`, and `।`, `॥` and `॰` become `.`. The function is idempotent.
- `script_of(text)` returns `latn` or `deva` for letters of one script, `mixed` for letters of more
  than one script, and `other` when there are no Latin or Devanagari letters (or no letters at all).
- `fold(text)` is the search form. It runs `canonical`, lowercases, strips diacritics, merges
  spelling variants and puts single spaces between words. A query gets the same fold as a document,
  so `kya`, `kyaa` and `क्या` all meet.

## Rules of `canonical`

1. **Letters.** Each consonant, independent vowel and matra has one Roman spelling in the table.
   Aspirates are written `kh gh chh jh th dh ph bh`, `श`/`ष` are `sh`, and `व` is `v`.
   - Nukta letters become the plain letter Hinglish writers use: `ज़` → `z`, `फ़` → `f`, `ड़` → `d`,
     `ढ़` → `dh`, `क़` → `k`, `ख़` → `kh`, `ग़` → `g`. The precomposed and decomposed forms give the
     same result.
   - `ऑ`/`ॉ` are `o`, and `ऍ`/`ॅ` are `e`. Zero-width joiners, avagraha and Vedic accents are
     dropped.
2. **Long vowels.** `आ/ा`, `ई/ी` and `ऊ/ू` are `aa`, `ee` and `oo` inside a word, and `a`, `i` and `u`
   at the end of a word (`काम` → `kaam`, `का` → `ka`).
   - Before a final nasal, `ī` is shortened (`नहीं` → `nahin`), while `ā` and `ū` stay long
     (`हाँ` → `haan`, `हूँ` → `hoon`).
   - A word that is only an independent vowel keeps it long (`आ` → `aa`).
3. **Conjuncts.** A virama joins consonants into one cluster (`क्या` → `kya`). A stop doubled with
   itself or its aspirate keeps one letter for the first half (`पक्का` → `pakka`,
   `अच्छा` → `acchha`, `बुद्धि` → `buddhi`).
4. **Nasals and visarga.**
   - Anusvara is `m` before `प फ ब भ म` and `n` everywhere else (`संपर्क` → `sampark`,
     `हिंदी` → `hindi`).
   - Chandrabindu is always `n`.
   - Visarga is `h`.
5. **Schwa.**
   - Word-final: the inherent `a` of the last letter is dropped (`कल` → `kal`). It is kept in a
     one-letter word (`न` → `na`) and after a final conjunct that ends in `य`, `र` or `व`
     (`मित्र` → `mitra`, `राज्य` → `raajya`).
   - Medial: the inherent `a` of a single consonant is dropped when the letter before it has a vowel
     and the letter after it is a consonant with a vowel (V C a C V). The scan runs right to left, so
     `समझना` → `samajhna` and `कमल` → `kamal`, never `kml`. Anything else keeps its `a`.
6. **Glide.** An independent `ए`/`ऐ` after `a`, `ā`, `i` or `ī` takes a `y` (`लिए` → `liye`,
   `गए` → `gaye`, `चाहिए` → `chaahiye`), but not after `u` (`हुए` → `hue`).
7. **Exceptions.** These are whole words whose rule output is not the usual Hinglish spelling. Keep
   the list short: `में` → `mein`.

## Rules of `fold`

`fold` applies the steps below to each word and repeats them until nothing changes:

- `ph`→`f`, `w`→`v`, `z`→`j`, `q`→`k`, `chh`/`chch`→`ch`, `ee`/`ii`→`i`, `oo`/`uu`→`u`;
- runs of the same letter become one letter (`kyaa` → `kya`, `pakka` → `paka`);
- a final `n` after a vowel is dropped in a word of three letters or more. This is how a written
  nasal meets an unwritten one (`nahin`/`nahi`, `haan`/`ha`).

These steps merge some English words too (`plan` → `pla`). Queries and documents are folded the
same way, so this costs only a little precision.

## v0 limits

- Not tuned or measured against the ASR eval set; Phase 2 does that.
- No Urdu (Arabic-script) output. That text is passed through unchanged and tagged `other` or
  `mixed`.
- The schwa rules are the two above and nothing more. Words that need epenthesis or a different
  deletion come out literal (`ख़त्म` → `khatm`, not `khatam`). English loanwords written in
  Devanagari come out as they are spelt (`ऑफ़िस` → `ofis`).
- Long vowels inside a word are always doubled (`राम` → `raam`), where many writers use one letter.
  `fold` merges both spellings.
