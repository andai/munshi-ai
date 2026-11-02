# andai/services/core/andai_core/minutes

Minutes of meeting generation (MOM-01..03). Pure code: no database, no routes. The pipeline
(andai-worker) calls `generate`, then stores the draft through `meetings/mom_writer.py`, which
applies the owner and date rules below.

- `templates.py` — the four templates (`standard`, `indian_formal_mom`, `standup`,
  `client_call`): name, section order, heading per section per output language (`en`, `hi`,
  `hinglish`) and prompt guidance. All fill the same `MomDocument` fields. Also the per-language
  "None recorded" and "Unassigned" labels and the output-language instructions.
- `prompt.py` — the system message (cite segment ids, omit what was not said, owner only when named
  with `ownerConfidence`, `dueText` as said, the language instruction, the template guidance) and
  the user message (meeting date, attendees, transcript lines); the reduce prompt for merging
  partial minutes.
- `chunking.py` — transcript lines `[seg_id] mm:ss Speaker: text_raw` (the script as spoken,
  D-50), packed in order into parts under a token budget; a segment too long for one part is split
  into several lines that keep its id. Tokens are estimated conservatively (Devanagari 0.5 per
  character, other text 0.3).
- `generate.py` — `generate(llm, segments, attendees, template, language, meeting_date)` → a
  `Generation` with the `Draft` (summary, decisions, action items, topics, risks with evidence; no
  ids, versions, profile or model) and the usage. One call when the transcript fits the context;
  otherwise one call per part and reduce calls that name which partial items each merged item comes
  from. andai computes merged evidence as the union of those items' evidence and keeps partial
  items the merge did not name, so nothing grounded is lost and nothing is truncated.
- `owners.py` — the owner rule (D-49): a named owner stays only when the model's confidence is at
  least 0.6 and the name matches exactly one attendee after folding (`प्रिया` = `Priya`, part of a
  name allowed when unambiguous, honorifics ignored); groups and several people are `unassigned`.
- `dates.py` — due phrases in English, Hindi and Hinglish (`kal`, `परसों`, `next Friday`,
  `agle hafte`, `3 din mein`, `20 November` ...) resolved deterministically against the meeting's
  local date; the rule list is in the module docstring. An unknown phrase gives no date.
