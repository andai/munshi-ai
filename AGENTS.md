# AGENTS.md — working in the munshi.ai repository

munshi.ai is a self-hosted, open-source AI meeting assistant for Google Meet, Microsoft Teams and
Zoom (English, Hindi, Hinglish), maintained by AND AI Platforms. This file is the contract for any
person or agent changing this repository. `andai/AGENTS.md` adds detail for the `andai/` tree.

## Repository map

| Path | What lives there |
|---|---|
| `core/` | The meeting platform: `gateway`, `identity` (admin-api), `meetings` (bot, meeting-api, transcription, MCP), `runtime` (container orchestration), `agent` (agent-api, worker), `flows` |
| `clients/` | Operator terminal (Next.js 15), browser extension, slim Python client |
| `services/dashboard` | Operator dashboard |
| `andai/` | munshi.ai's own services (`services/core`, `worker`, `intake`), the user portal (`portal`, Next.js 16), the compose overlay, image catalogue and operator scripts |
| `deploy/` | Docker Compose (default) and Helm |
| `docs/` | Docs site sources (`docs/docs`), ADRs, generated architecture views |
| `architecture.calm.json` | The architecture chart; sealed in `architecture.seal.json` |
| `scripts/gates.mjs` | The gate suite (structure, contracts, licenses, isolation, docs) |

Bring the whole stack up with `make -C andai up` (see `andai/docs/dev-vm.md`).

## Branches and pull requests

- `main` is the default and protected branch: it changes only through a pull request from
  `development`. No direct pushes, no force-pushes, no deletion.
- Cut every work branch from the latest `development` (`feat/…`, `fix/…`, `docs/…`), keep it to one
  change, and merge it into `development` (`--no-ff`) or open a pull request against `development`.
- Never edit files another person or session has left uncommitted in a checkout; work on your own
  branch and surface the conflict instead.

## Commits

- Every commit is GPG-signed (`commit.gpgsign=true`); GitHub must be able to verify it.
- Write the message as what the change does and why. Do not mention planning ids (phase, plan or
  task numbers) unless asked.
- AI co-author trailers are allowed: when an AI assistant helped, end the message with its
  `Co-Authored-By:` line. No DCO sign-off is required.
- Never commit secrets: `.env` files, keys, tokens, or values from the operator's environment.
  The root `.env.example` holds placeholders only.
- Every environment variable a container reads is listed in the root `.env.example` with a description;
  `make -C andai env-check` (gate:env-example, pre-push) fails otherwise. After adding or moving a key,
  run `make -C andai env-example` to regenerate its `# used by:` lines.

## Gates before you push

- The pre-push hook (`.githooks/pre-push`) runs the fast static gates; the full suite is
  `node scripts/gates.mjs all` (or `pnpm gates`). Do not push red, and do not bypass the hook.
- Changed `architecture.calm.json`? Run `pnpm seal:arch` in the same change. Changed a sealed
  contract schema? Back-compatible → `pnpm seal:contracts`; breaking → add the next version (`vN+1`).
  Changed the platform's DB models? `pnpm seal:schema`.
- New or changed image reference? It must be in `andai/images.json`; `make -C andai images-check`
  enforces it. Images are built from this repo and published as `ghcr.io/andai/munshi-<name>:<VERSION>`.
- Green gates are necessary, not sufficient: prove user-facing behaviour at the level of the claim
  (a live run for live behaviour, a browser run for UI).

## Licensing

- Code here is Apache-2.0 (`LICENSE`); attribution and third-party notices live in `NOTICE` and
  `THIRD_PARTY_LICENSES.md`. Keep them accurate.
- New dependencies must be permissive (MIT, BSD, Apache-2.0, ISC). Weak copyleft (LGPL, MPL) needs an
  entry in `license-exceptions.json`. Never add GPL, AGPL, SSPL, BUSL or non-commercial licenses.
  Anything proprietary needs the CTO's explicit approval first.

## Product rules that code must keep

- **Data stays in-house.** No hosted third-party endpoints in the data path: speech-to-text and
  language models go to an endpoint the operator runs. Never add telemetry, analytics or "phone
  home" calls.
- **One name.** The product, code, configs, images, docs and prompts say munshi (or andai for AND AI's
  own services). Never introduce the name of the project this codebase started from; the only
  permitted mention is the attribution in `NOTICE`.
- **Org isolation and least privilege.** Every org-scoped read or write is checked; secrets are
  write-only and encrypted at rest; admin credentials never reach a browser.
- **Consent.** The meeting bot always identifies itself as a recorder.

## How to work

- **Expect before you act.** Write the expected result, run it, record the actual result (commands,
  outputs, counts — and what you did not check), then the verdict. An unexpected result is a finding
  to explain, never something to paper over.
- **Fix where a defect is introduced,** not where it is observed; reproduce it without a live meeting
  first when you can.
- **The core owns its contracts;** clients adapt at their own boundary.
- **Source describes the present design.** No history or bug archaeology in comments.
- **Report facts first,** then your interpretation, labelled as yours.

## Planning

Roadmap, specs and plans are kept with the GSDD workflow in `.work/` (git-ignored, local to the
maintainers' machines). The workflow skills live in `.agents/skills/` and `.claude/`. Lifecycle:
`gsdd-new-project → gsdd-plan → gsdd-execute → gsdd-verify → gsdd-audit-milestone`. Implement only
what the approved plan or a direct request says; record unrelated ideas instead of doing them.

## Security issues

Report vulnerabilities privately through GitHub Security Advisories on `andai/munshi-ai`
(see `SECURITY.md`), never in a public issue.
