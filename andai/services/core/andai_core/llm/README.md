# andai/services/core/andai_core/llm

The language model andai writes minutes with (MOM-01, D-47): one deployment-wide OpenAI-compatible
route inside the perimeter, configured only from env (`ANDAI_LLM_BASE_URL` without `/v1`,
`ANDAI_LLM_API_KEY`, `ANDAI_LLM_MODEL`, `ANDAI_LLM_CONTEXT_TOKENS`, `ANDAI_LLM_MAX_OUTPUT_TOKENS`,
`ANDAI_LLM_TIMEOUT_S`, `ANDAI_LLM_PART_TOKENS`). There is no default host; an empty base URL turns minutes off. Plain
`httpx`, no SDK.

- `client.py` — `LlmClient.chat_json(messages, schema, *, name)`: `POST <base>/v1/chat/completions`
  with `response_format: json_schema` (`strict: true`) built from a pydantic model by
  `strict_schema`; the reply is parsed strictly and validated against the model, with one repair
  retry. Typed failures: `LlmNotConfigured`, `LlmUnavailable` (network, timeout, error status),
  `LlmInvalidOutput` (no schema-valid JSON after the retry), `LlmTruncated`
  (`finish_reason == "length"`). The result carries the usage (tokens, latency, calls) for
  metrics. `LlmConfig.profile_id` is the deployment profile id stored as `llmProfileId`
  (`uuid5` of base URL and model). The key is a `SecretStr`; errors and log lines name the call,
  status and error type only, never the key, the host or any content.
  `load_live_settings(env_file)` reads a route from an operator env file for live tools and live
  tests (`ANDAI_LLM_*`, or the stack's `MUNSHI_LLM_*` route with its `/v1` stripped).
- `__main__.py` — `python -m andai_core.llm probe --env-file <abs> --out <file>`: one minutes call
  on a tiny synthetic transcript; writes `schema_valid`, the model id, latency and token counts
  (never the key or host). Synthetic text only until the route is verified AND AI-hosted (D-21).
