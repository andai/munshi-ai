# AGENTS.md — andai/

`andai/` holds munshi.ai's own overlay code. The whole repository is munshi.ai's codebase (D-25):
there is no upstream to stay in sync with, so any file in the tree may be changed directly, with
normal review. Attribution for the code this project started from is in the root `NOTICE`.

- **Services:** `andai/services/{core,worker,intake}` (Python 3.12, one uv workspace at
  `andai/pyproject.toml`).
- **Portal:** `andai/portal` (Next.js).
- **Deployment:** `andai/deploy/` (compose overlay, env template, hardened agent-worker image);
  bring-up is `make -C andai up` (see `andai/Makefile` and `andai/docs/dev-vm.md`).
- **Images:** `andai/images.json` lists every image the stack runs; all are built from this repo
  (`make -C andai images`) as `ghcr.io/sunydepalpur/munshi-<name>:<VERSION>` (root `VERSION`).
  A new service or image reference must be added there; `make -C andai images-check` enforces it.
- **Operator scripts and tools:** `andai/scripts/`, `andai/tools/`.
- **Root `CLAUDE.md` and `AGENTS.md`** describe the meeting platform under `core/`, `clients/`,
  `services/` and `deploy/`; follow them when you work there.
- **Planning** state is in `.work/` (GSDD, git-ignored). It changes only through the GSDD workflows.

Quick checks:

```bash
uv run --directory andai pytest            # andai Python services
npm --prefix andai/portal ci && npm --prefix andai/portal run build
```
