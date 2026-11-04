# andai/services/core/andai_core/minutes

Minutes of meeting generation (MOM-01..03) and their downloads (MOM-04). Pure code: no database,
no routes. The pipeline
(andai-worker) calls `generate`, then stores the draft through `meetings/mom_writer.py`, which
applies the owner and date rules below.

- `templates.py` — the four templates (`standard`, `indian_formal_mom`, `standup`,
  `client_call`): name, section order, heading per section per output language (`en`, `hi`,
  `hinglish`) and prompt guidance. All fill the same `MomDocument` fields. Also the per-language
  "None recorded" and "Unassigned" labels and the output-language instructions.
- `prompt.py` — the system message (cite the numbers of the transcript lines, omit what was not said, owner
  only when named with `ownerConfidence` (not a group, nor someone who only raised, discussed or
  organised the task for others — scheduling it, logging it, finding someone to do it; null when
  nobody has taken it), `dueText` as said for this item, no decision or action
  item from a suggestion nobody agreed to, the language instruction, the template guidance) and
  the user message (meeting date, attendees, transcript lines); the reduce prompt, which shows each
  partial item as one referenced line and asks only for a summary and the groups of items that say
  the same thing.
- `chunking.py` — transcript lines `[n] mm:ss Speaker: text_raw` (`n` the segment's number in the
  meeting, mapped back to its id by `refs`; the text as spoken, D-50), packed in order into parts
  under a token budget; a segment too long for one part is split into several lines that keep its
  number. Tokens are estimated conservatively (Devanagari 0.5 per
  character, other text 0.3).
- `generate.py` — `generate(llm, segments, attendees, template, language, meeting_date)` → a
  `Generation` with the `Draft` (summary, decisions, action items, topics, risks with evidence; no
  ids, versions, profile or model) and the usage. Cited line numbers become segment ids as each
  reply arrives. One call when the transcript fits one part (`ANDAI_LLM_PART_TOKENS`, 5000 estimated
  tokens by default, or less when the context is smaller), so that no call runs long; otherwise one
  call per part and reduce calls that return only a summary and groups of duplicate items. andai
  combines each group (first item's text, union of the evidence, most confident owner, first due
  phrase) and keeps every other partial item, so nothing grounded is lost and nothing is truncated.
- `owners.py` — the owner rule (D-49): a named owner stays only when the model's confidence is at
  least 0.6 and the name matches exactly one attendee after folding (`प्रिया` = `Priya`, part of a
  name allowed when unambiguous, honorifics ignored), or failing that on the consonant skeleton
  (`अय्यर` = `Iyer`; words of three letters or more); groups and several people are `unassigned`.
- `dates.py` — due phrases in English, Hindi and Hinglish (`kal`, `परसों`, `next Friday`,
  `agle hafte`, `3 din mein`, `20 November` ...) resolved deterministically against the meeting's
  local date; the rule list is in the module docstring. An unknown phrase gives no date. A count
  before something (`2 days before 20 October`, `शिविर से दो दिन पहले`) is the stated anchor date
  minus the count (a range counts its larger number); with no anchor date in the phrase, or a
  result before the meeting date, it gives no date, never the meeting date plus the count (D-52).
- `eval.py` — the gold-set evaluator (MOM-02, D-45): loads and validates the synthetic fixtures in
  `tests/mom_gold/`, runs each through `generate` and the writer's `build_document` (no database),
  scores the stored document against its labels (recall, labelled support within ±2 segments,
  owners, due dates, needles, truncation, invalid ids before the filter) and writes a JSON and
  Markdown report with the SC3 verdict per criterion and latency per length band.
  `python -m andai_core.minutes.eval validate <gold dir>` lists problems in the fixtures.
- `render.py` — a stored `MomDocument` as Markdown (`markdown`) or as the standalone HTML the PDF is
  printed from (`html`): the template's sections in order under its headings in the document's
  language, "None recorded" for an empty section, each item's evidence as transcript times (mm:ss);
  every value escaped, no links or images. `filename` gives an ASCII-only download name.
- `pdf.py` — `render(html)` prints with WeasyPrint, imported only when called (andai-worker has no
  Pango). The URL fetcher refuses every URL; fonts come from the image (`fonts/README.md`).
  `PdfUnavailable` where WeasyPrint cannot load.
