#!/usr/bin/env python3
"""env_example.py — keep the root .env.example complete and its `# used by:` lines true.

The root `.env.example` is the one template for the whole stack (`make -C andai up`). Every key in
it carries a generated line directly above it naming what reads it: the compose services that
interpolate it, `docker compose` itself, and the andai Makefile or scripts. Humans write the
descriptions; this tool owns the `# used by:` lines.

    python3 -I env_example.py --compose-json STACK.json --env-example .env.example --check
    python3 -I env_example.py --compose-json STACK.json --env-example .env.example --write

STACK.json is `docker compose … config --no-interpolate --format json` of the full stack with every
profile enabled (`make -C andai env-check` / `env-example` produce it), so the `${VAR}` references
are still visible per service.

--check fails when:
  * a variable a service interpolates is not in the template (as `KEY=` or a commented `#KEY=`),
  * a key in the template is read by nothing (no service, not compose, not the andai tooling),
  * a `# used by:` line is missing or differs from what --write would generate.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")
KEY_RE = re.compile(r"^#?\s?([A-Z][A-Z0-9_]*)=")
USED_BY = "# used by: "
# Read by docker compose itself, not interpolated into any service.
COMPOSE_KEYS = {"COMPOSE_PROJECT_NAME": "docker compose (project name)",
                "COMPOSE_PROFILES": "docker compose (profiles)"}
# Set by andai/Makefile on every compose call; never belongs in an env file.
MAKE_SET = {"ANDAI_ROOT"}
TOOLING = {
    "andai/Makefile": "make -C andai",
    "andai/scripts/gen-secrets.sh": "gen-secrets.sh",
    "andai/scripts/live_dispatch.py": "live-dispatch",
    "andai/scripts/health.sh": "make -C andai health",
}


def service_refs(stack: dict) -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {}
    for name, svc in stack.get("services", {}).items():
        for var in VAR_RE.findall(json.dumps(svc)):
            refs.setdefault(var, set()).add(name)
    top = {k: v for k, v in stack.items() if k != "services"}
    for var in VAR_RE.findall(json.dumps(top)):
        refs.setdefault(var, set()).add("docker compose")
    return refs


def tooling_refs(keys: set[str]) -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {}
    for path, label in TOOLING.items():
        f = ROOT / path
        if not f.exists():
            continue
        text = f.read_text()
        for key in keys:
            if re.search(rf"\b{re.escape(key)}\b", text):
                refs.setdefault(key, set()).add(label)
    return refs


def template_keys(lines: list[str]) -> list[tuple[int, str]]:
    """(line index, key) for every `KEY=` or commented `#KEY=` line."""
    found = []
    for i, line in enumerate(lines):
        m = KEY_RE.match(line)
        if m and not line.startswith("# used by"):
            found.append((i, m.group(1)))
    return found


def used_by_line(key: str, svc: dict[str, set[str]], tools: dict[str, set[str]]) -> str | None:
    parts: list[str] = []
    services = sorted(svc.get(key, set()) - {"docker compose"})
    if services:
        parts.append(", ".join(services))
    if key in COMPOSE_KEYS:
        parts.append(COMPOSE_KEYS[key])
    elif "docker compose" in svc.get(key, set()):
        parts.append("docker compose")
    parts.extend(sorted(tools.get(key, set())))
    return USED_BY + "; ".join(parts) if parts else None


def render(lines: list[str], svc: dict[str, set[str]], tools: dict[str, set[str]]) -> list[str]:
    out: list[str] = []
    for line in lines:
        if line.startswith(USED_BY):
            continue
        m = KEY_RE.match(line)
        if m:
            ub = used_by_line(m.group(1), svc, tools)
            if ub:
                out.append(ub)
        out.append(line)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--compose-json", required=True)
    ap.add_argument("--env-example", required=True)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    a = ap.parse_args()

    stack = json.load(sys.stdin if a.compose_json == "-" else open(a.compose_json))
    path = Path(a.env_example)
    lines = path.read_text().splitlines()
    keys = {k for _, k in template_keys(lines)}
    svc = service_refs(stack)
    tools = tooling_refs(keys)

    problems: list[str] = []
    for var in sorted(set(svc) - keys - MAKE_SET):
        problems.append(f"{var}: read by {', '.join(sorted(svc[var]))} but missing from {path.name}")
    for key in sorted(keys):
        if key not in svc and key not in tools and key not in COMPOSE_KEYS:
            problems.append(f"{key}: in {path.name} but read by no service, compose or andai tooling")

    rendered = render(lines, svc, tools)
    if a.write:
        path.write_text("\n".join(rendered) + "\n")
        for p in problems:
            print(f"env-example: {p}", file=sys.stderr)
        print(f"env-example: wrote {len(keys)} keys with used-by lines → {path}")
        return 1 if problems else 0

    if rendered != lines:
        problems.append(f"`# used by:` lines are stale — run `make -C andai env-example`")
    if problems:
        for p in problems:
            print(f"✗ env-example: {p}", file=sys.stderr)
        return 1
    print(f"✓ env-example: {len(keys)} keys, every service variable documented, used-by lines current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
