# andai/services/core/andai_core/search

Keyword search over the meetings a caller may read (PORT-05): `GET /api/v1/search?q=&limit=&cursor=`.

- `routes.py` — the route. Any member; `q` needs at least two letters or digits after folding and
  at most 200 characters (else 422 `bad_query`); `limit` is 1–50 (default 20); `cursor` continues
  the same query (else 422 `bad_cursor`).
- `service.py` — the search:
  - **Query.** Folded like the documents (`andai_core.text.fold`), so a Roman query finds speech
    stored in Devanagari (`kal` finds `कल`) and spelling variants meet (`kya`, `kyaa`, `KYA`).
    Search syntax is folded away; the words are ANDed.
  - **Texts.** Stored transcript segments, the meeting title and the latest minutes version. All
    three match on whole folded words (`tsvector @@ websearch_to_tsquery('simple', …)`); titles and
    minutes also match close spelling variants (`pg_trgm` strict word similarity ≥ 0.5).
  - **Access.** `meetings/access.accessible(actor)` is in the SQL of every text's arm, before
    grouping, ranking, counting and the page limit. Results and `total` cover readable meetings
    only.
  - **Order and pages.** Best match first (title above minutes and transcript, whole words above
    variants), then newest first, then id; keyset cursor.
  - **Answer.** `{query, total, results, next_cursor}`. Each result: id, title, platform, status,
    team, start and creation times, and up to three `hits` (title, then the best transcript
    segments, then the minutes). Each hit has `text` with `marks` (`[start, end)` offsets of the
    matched words), cut to about 240 characters around the first mark. A transcript hit also has
    `segment_id`, `speaker`, `start` and the original-script `text_raw` with `raw_marks`.

**Cost under row-level security.** The app role reads under FORCE ROW LEVEL SECURITY. Postgres
checks each row's policy before any condition whose operator is not LEAKPROOF, and `@@` and `<<%`
are not, so no GIN index serves the app role. Search reads every segment of the caller's org. Each
segment's `search_tsv` is stored (migration 0008), so the scan parses no text: about 25 ms per
200,000 segments. At 1,000 meetings × 200 segments the API answers with p95 under 100 ms
(`tests/test_search.py`). The cost grows linearly with an org's stored segments. Trigram tests cost
about 2 µs per row, so they run on titles and minutes only.

Every new path that hides a meeting from reads (deletion) must hide it here too. It does so by
changing `accessible`, which every arm already uses.
