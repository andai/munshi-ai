"""Test instruments that ship in the andai-core image but never run in a default stack.

`fake_llm` is a deterministic OpenAI-compatible chat route for tests and the e2e compose profile.
`load` is the load driver for a test stack with the mock bot (andai/docs/load-test.md).
`alert_sink` stands in for Slack and Teams webhooks and scans what it caught for PII.
"""
