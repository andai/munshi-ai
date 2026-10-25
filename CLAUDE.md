@AGENTS.md

## Claude Code specifics

- Monorepo: pnpm + turbo for the TypeScript packages (`pnpm install`, `pnpm test`), uv for Python
  services (`uv run --directory <project> pytest`). munshi.ai's own Python services share one uv
  workspace at `andai/pyproject.toml`.
- UIs: the user portal is `andai/portal` (Next.js 16, Tailwind, shadcn/ui); the operator terminal is
  `clients/terminal` (Next.js 15, port 3000). Prove UI changes in a browser.
- Planning work goes through the GSDD skills (`/gsdd-plan`, `/gsdd-execute`, `/gsdd-verify`); planning
  files stay in `.work/` and are never committed.
- Commits are GPG-signed and end with the `Co-Authored-By:` line for the assistant; work on a branch
  from `development`, never on `main`.
