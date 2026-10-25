# Contributing to munshi.ai

Thanks for helping. munshi.ai is licensed under the Apache License 2.0, and so is every
contribution: by opening a pull request you agree that your change is licensed under it
(Apache-2.0 section 5). There is no CLA and no sign-off requirement.

## Branch flow

`feature` → `development` → `main`

1. Cut a branch from the latest `development` (`feat/...`, `fix/...`, `docs/...`).
2. Open the pull request against `development`, never against `main`.
3. `main` only receives reviewed merges from `development` when a release is cut.

## Simple rules

- Keep a pull request to one change, and say what it does and why.
- Add or update tests with the change; run the affected package's tests before pushing
  (`pnpm --filter <package> test` for Node packages, `uv run pytest` inside a Python project,
  `uv run --directory andai pytest` for the munshi.ai services).
- New dependencies must be permissively licensed (MIT, BSD, Apache-2.0); `pnpm gate:licenses`
  checks this.
- Never commit secrets, real meeting audio, transcripts or personal data.
- munshi.ai's own services live under `andai/`; see [`andai/AGENTS.md`](andai/AGENTS.md).

Security issues are not reported in public issues or pull requests; see [SECURITY.md](SECURITY.md).
