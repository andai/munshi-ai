# andai/services/core/tests/mom_gold

The minutes gold set (MOM-02, D-45): 40 synthetic, scripted meetings with labelled decisions, action
items, owners, due dates, distractors and, in long meetings, a needle said once at minute 45. Every
name, number and meeting in them is invented; nothing comes from a real meeting or real speech.

`andai_core.minutes.eval` loads, validates, runs and scores them. `test_mom_gold.py` checks the set
offline (schema, counts, labels, the scorer); `test_mom_gold_live.py` (`-m live`) runs all 40
through the configured LLM route and writes the report under `.work/phases/06-mom-pipeline/live/`.

## Layout

```
mom_gold/
  en/        14 fixtures: 12 short (hand-written) + 2 long (generated: 60 and 90 min)
  hi/        13 fixtures: 11 short (Devanagari)   + 2 long (generated: 60 and 90 min)
  hinglish/  13 fixtures: 11 short (Roman, mixed) + 2 long (generated: 60 and 60 min)
  build_long.py   the seeded generator of the six long fixtures
```

File names are `NN-<slug>.json`; the `id` field equals the file name without `.json`. Long
fixtures are numbered `90` and up and are written only by `build_long.py`:

- `uv run --directory andai python services/core/tests/mom_gold/build_long.py` writes them;
- `--check` regenerates them in memory and fails unless the files match byte for byte;
- `--count` prints the totals per language, the long meetings and the needles.

`uv run --directory andai python -m andai_core.minutes.eval validate services/core/tests/mom_gold`
lists every problem in the set (or in the files given after the directory).

## Schema

```jsonc
{
  "id": "01-pricing-review",
  "language": "en",                 // en | hi | hinglish: the speech and the minutes' language
  "kind": "short",                  // short (5-15 min, 40-120 segments) | long (60 or 90 min)
  "title": "Pricing review",
  "meeting_date": "2026-11-04",     // the meeting's local date (due dates resolve against it)
  "duration_min": 8,
  "attendees": ["Meera Iyer", "Kabir Sethi", "Tanvi Rao"],
  "segments": [
    {"id": "s001", "start_s": 0, "speaker": "Meera Iyer", "text": "..."}
  ],
  "labels": {
    "decisions": [
      {"id": "D1", "fact": "The launch moves to 15 December.",
       "match": [["launch", "लॉन्च"], ["15"], ["december", "दिसंबर"]],
       "evidence": ["s014"]}
    ],
    "action_items": [
      {"id": "A1", "fact": "Kabir sends the revised price sheet by Friday.",
       "match": [["price sheet", "pricing sheet"]],
       "owner": "Kabir Sethi",       // an attendee, or "unassigned"
       "due": "2026-11-06", "due_phrase": "by Friday",   // both null when no due time is said
       "evidence": ["s021", "s022"]}
    ],
    "distractors": [
      {"id": "X1", "kind": "not_agreed", "fact": "A discount for annual plans was suggested, not agreed.",
       "match": [["discount"], ["annual"]], "evidence": ["s030"]}
    ]
  }
}
```

- `segments[].id` are local ids `s001`, `s002` ... in order; the evaluator turns them into
  production-shaped ids (`seg_` + 24 hex) before the model sees them. `start_s` rises; every
  speaker is an attendee; no segment text appears twice.
- `match` decides whether an item of the minutes is this label: **every group must have one
  alternative** whose words all occur in the item's text. Text is folded first
  (`andai_core.text.fold`: Devanagari is transliterated, case and diacritics dropped), words of six or
  more letters match at a 0.8 similarity ratio or as a prefix, shorter words as a prefix, numbers
  exactly. Each group is one fact-bearing idea
  (an object, a number, a name), with alternatives for the ways the minutes may say it in the
  fixture's output language: English words, Devanagari, Roman Hindi, synonyms. Avoid generic words
  (`team`, `meeting`, `will`). The validator requires the groups to occur in the label's own
  evidence (±2 segments), so labels stay tied to what was said.
- `evidence` are the segments where the fact is said or agreed. A minutes item that matches the
  label is *supported* when it cites one of them or a segment within ±2 of one.
- `owner` follows D-49: a person named as the owner who is an attendee; `unassigned` when the
  transcript names nobody ("someone", "the team", "we"), or a person who is not an attendee.
- `due` follows D-51 against `meeting_date`: "next <weekday>" / "agle <weekday>" is that weekday
  of the following calendar week (weeks start on Monday); a bare weekday ("Friday", "Monday tak")
  is the first such day after the meeting date; `kal` / tomorrow is +1, `parso` +2; counts of days
  or weeks add to the date. `due_phrase` is the phrase as said, and the validator checks that
  `minutes.dates.resolve(due_phrase, meeting_date)` gives `due`.
- `distractors` are things the minutes must not turn into decisions or action items:
  `not_agreed` (a suggestion nobody agreed to; the run reports any decision or action item that
  matches one), `other_date` (a date said near an action item that belongs to something else).
- Long meetings mark one decision or action item `"needle": true`: its evidence is at minute 45
  (44:00-45:59) and no other segment says it.

## Authoring rules

- Invented sample names only: no real people, companies, products, phone numbers or meeting codes.
- Short fixtures: 5-15 min, 40-120 segments, 3-6 labelled decisions and action items, at least one
  decision and one action item, one action item without a named owner, one with a due date, one
  `not_agreed` and one `other_date` distractor. Conversations read like real meetings: status,
  questions, disagreement, side topics, a suggestion dropped, an owner named only as "someone".
- Hindi fixtures are Devanagari speech (English terms as Indians say them may stay in Roman);
  Hinglish fixtures mix Roman Hindi and English, some with Devanagari words mixed in.
- A label checks the fact, not the words: the needle and the labels are phrased differently from
  the segments where possible, and `match` lists the alternatives.
