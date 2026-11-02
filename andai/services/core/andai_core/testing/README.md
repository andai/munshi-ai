# andai/services/core/andai_core/testing

Test instruments shipped in the andai-core image that no default stack runs:

- `fake_llm.py` — a deterministic OpenAI-compatible `POST /v1/chat/completions` (and `/healthz`).
  It answers andai's minutes prompts with schema-valid drafts built from the prompt itself: one
  decision, topic and risk citing transcript segment ids, one action item owned by the first
  attendee ("kal" due), one with no owner, in the requested output language (Devanagari for `hi`).
  Tests use it in-process (`httpx.ASGITransport`); the e2e compose profile runs it as the
  `fake-llm` service (`python -m andai_core.testing.fake_llm`).
