# andai/services/core/andai_core/testing

Test instruments shipped in the andai-core image that no default stack runs:

- `fake_llm.py` — a deterministic OpenAI-compatible `POST /v1/chat/completions` (and `/healthz`).
  It answers andai's minutes prompts with schema-valid drafts built from the prompt itself: one
  decision, topic and risk citing transcript segment ids, one action item owned by the first
  attendee ("kal" due), one with no owner, in the requested output language (Devanagari for `hi`).
  Tests use it in-process (`httpx.ASGITransport`); the e2e compose profile runs it as the
  `fake-llm` service (`python -m andai_core.testing.fake_llm`).
- `load.py` — the load driver (`python -m andai_core.testing.load`, `make -C andai load-test`):
  dispatches N mock meetings that play the mock bot's `hold` scenario, samples how many are in
  `recording` at once, checks that dispatches over the runtime's bot cap fail as
  `capacity_exhausted`, stops them all and writes a report of counts and timings
  (`andai/docs/load-test.md`). Runs on the operator's machine against a test stack, not in a
  service.
