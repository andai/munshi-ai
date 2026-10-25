#!/usr/bin/env bash
# run-suites.sh OUT.json [--only SUBSTR]... [--skip-install] — run every unit test suite in the repo
# and write one JSON result per suite to OUT.json (a JSON array).
#
# Discovery (tracked files only; node_modules, .venv and .work/ are never looked at):
#   node  every tracked package.json with a `test` script, except the repo-root aggregator
#         (`turbo run test`, which only re-runs the packages below). Members of the pnpm workspace
#         are installed by one `pnpm install --frozen-lockfile` at the root and built once with
#         `turbo run build` (turbo's `test` task depends on `build`); packages outside the
#         workspace that carry their own package-lock.json are installed with `npm ci`.
#   uv    every tracked pyproject.toml whose directory has a tests/ folder, run with
#         `uv run --frozen --directory <dir> pytest -q -p no:cacheprovider --junitxml=...`
#         (--frozen: lockfiles are used as-is and never rewritten).
#
# A node `test` script that is a chain `a && b && c` is run one command at a time (same PATH as
# `pnpm run` / `npm run`: the package's and the root's node_modules/.bin first), so every failing
# test file is reported, not only the first. `tsx FILE` / `node FILE` commands are reported by FILE;
# `vitest run` commands add a JUnit reporter and are reported per test case.
#
# Each result: {suite, dir, kind: pnpm|uv, runner, status: pass|fail|not-runnable,
#               failing_tests: [...], seconds, reason?}
# `not-runnable` means the suite could not start (install failed, toolchain missing, no JUnit
# report produced); `reason` holds the first line of the error.
#
# Per-suite logs go to OUT's directory as <OUT stem>.logs/<suite>.log.
# SUITE_TIMEOUT (seconds, default 1200) bounds each command; a timeout counts as a failure.
set -euo pipefail

if [[ $# -lt 1 || "$1" == -* ]]; then
  echo "usage: $0 OUT.json [--only SUBSTR]... [--skip-install]" >&2
  exit 2
fi

ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 2; }

exec python3 -I - "$ROOT" "$@" <<'PY'
import json, os, re, shlex, signal, subprocess, sys, tempfile, time
import xml.etree.ElementTree as ET

root = sys.argv[1]
out_path = os.path.abspath(sys.argv[2])
only, skip_install = [], False
args = sys.argv[3:]
i = 0
while i < len(args):
    if args[i] == "--only":
        only.append(args[i + 1]); i += 2
    elif args[i] == "--skip-install":
        skip_install = True; i += 1
    else:
        sys.exit(f"unknown argument: {args[i]}")

TIMEOUT = int(os.environ.get("SUITE_TIMEOUT", "1200"))
LOG_CAP = 200_000
log_dir = os.path.splitext(out_path)[0] + ".logs"
os.makedirs(log_dir, exist_ok=True)
tmp = tempfile.mkdtemp(prefix="run-suites-")


def say(msg):
    print(msg, file=sys.stderr, flush=True)


def first_line(text):
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:300]
    return "no output"


def run(cmd, cwd, log, env=None, shell=False):
    """Run cmd; return (exit_code or None on timeout, combined output). Appends to log."""
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, shell=shell, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True, text=True, errors="replace")
    try:
        output, _ = proc.communicate(timeout=TIMEOUT)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        output, _ = proc.communicate()
        code = None
    with open(log, "a") as fh:
        fh.write(f"$ {cmd if isinstance(cmd, str) else shlex.join(cmd)}  (cwd={cwd})\n")
        text = output or ""
        if len(text) > LOG_CAP:  # some suites print megabytes; keep the tail, where failures are
            text = f"[... {len(text) - LOG_CAP} chars truncated ...]\n" + text[-LOG_CAP:]
        fh.write(text)
        fh.write(f"\n[exit {code if code is not None else 'TIMEOUT'}]\n\n")
    return code, output or ""


def junit_failures(path):
    failing = []
    tree = ET.parse(path)
    for case in tree.iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            cls = case.get("classname") or case.get("file") or ""
            failing.append(f"{cls}::{case.get('name')}" if cls else case.get("name"))
    return sorted(set(failing))


def tracked(pattern):
    res = subprocess.run(["git", "ls-files", "-z", "--", pattern], cwd=root,
                         capture_output=True, text=True, check=True)
    keep = []
    for p in res.stdout.split("\0"):
        if not p:
            continue
        parts = p.split("/")
        if "node_modules" in parts or ".venv" in parts or parts[0] == ".work":
            continue
        keep.append(p)
    return sorted(keep)


def selected(suite, d):
    return not only or any(s in suite or s in d for s in only)


def log_for(suite):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", suite).strip("_") or "root"
    path = os.path.join(log_dir, name + ".log")
    if os.path.exists(path):
        os.remove(path)
    return path


# ---------- discovery ----------
node_suites = []
for pj in tracked("*package.json"):
    d = os.path.dirname(pj) or "."
    try:
        data = json.load(open(os.path.join(root, pj)))
    except (OSError, ValueError):
        continue
    script = (data.get("scripts") or {}).get("test")
    if not script:
        continue
    if d == "." and script.strip().startswith("turbo "):
        continue  # repo-root aggregator: re-runs the package suites below
    node_suites.append({"suite": data.get("name") or d, "dir": d, "script": script})

uv_suites = []
for pp in tracked("*pyproject.toml"):
    d = os.path.dirname(pp) or "."
    if os.path.isdir(os.path.join(root, d, "tests")):
        uv_suites.append({"suite": d, "dir": d})

node_suites = [s for s in node_suites if selected(s["suite"], s["dir"])]
uv_suites = [s for s in uv_suites if selected(s["suite"], s["dir"])]
say(f"discovered {len(node_suites)} node suites, {len(uv_suites)} uv suites")

# ---------- node installs ----------
ws = subprocess.run(["pnpm", "ls", "-r", "--depth", "-1", "--json"], cwd=root,
                    capture_output=True, text=True)
workspace_dirs = set()
if ws.returncode == 0:
    for pkg in json.loads(ws.stdout or "[]"):
        workspace_dirs.add(os.path.relpath(pkg["path"], root))

install_error = {}  # dir -> reason ("*" = the pnpm workspace)
if node_suites and not skip_install:
    if any(s["dir"] in workspace_dirs for s in node_suites):
        say("pnpm install --frozen-lockfile")
        code, out = run(["pnpm", "install", "--frozen-lockfile"], root, os.path.join(log_dir, "_pnpm-install.log"))
        if code != 0:
            err = [l for l in out.splitlines() if "ERR" in l] or out.splitlines()[-1:] or [""]
            install_error["*"] = "pnpm install failed: " + first_line(err[0])
        else:
            # turbo's `test` task depends on `build` (workspace packages import each other's
            # dist/), so build once the way `pnpm test` would. A build failure is not fatal here:
            # the affected suites fail on their own and the log says why.
            say("turbo run build --continue")
            code, _ = run(["pnpm", "exec", "turbo", "run", "build", "--continue"], root,
                          os.path.join(log_dir, "_turbo-build.log"))
            if code != 0:
                say("  build had failures; see _turbo-build.log")
    for s in node_suites:
        if s["dir"] in workspace_dirs:
            continue
        lock = os.path.join(root, s["dir"], "package-lock.json")
        cmd = ["npm", "ci", "--no-audit", "--no-fund"] if os.path.exists(lock) else ["npm", "install", "--no-audit", "--no-fund"]
        say(f"{shlex.join(cmd)} in {s['dir']}")
        code, out = run(cmd, os.path.join(root, s["dir"]), log_for("_npm-install-" + s["dir"]))
        if code != 0:
            err = [l for l in out.splitlines() if "ERR" in l] or out.splitlines() or [""]
            install_error[s["dir"]] = "npm install failed: " + first_line(err[0])

# ---------- run ----------
results = []


def split_chain(script):
    if any(tok in script for tok in ("||", ";", "|", "`", "$(")):
        return [script.strip()]
    return [p.strip() for p in script.split("&&") if p.strip()]


for s in node_suites:
    d = s["dir"]
    pkg_dir = os.path.join(root, d)
    in_ws = d in workspace_dirs
    runner = "pnpm" if in_ws else "npm"
    log = log_for(s["suite"])
    start = time.time()
    res = {"suite": s["suite"], "dir": d, "kind": "pnpm", "runner": runner,
           "status": "pass", "failing_tests": [], "seconds": 0}
    err = install_error.get("*" if in_ws else d)
    if err is None and not os.path.isdir(os.path.join(pkg_dir, "node_modules")):
        err = "node_modules missing (dependencies not installed)"
    if err:
        res.update(status="not-runnable", reason=err)
        results.append(res)
        say(f"[not-runnable] {s['suite']}: {err}")
        continue
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([os.path.join(pkg_dir, "node_modules", ".bin"),
                                   os.path.join(root, "node_modules", ".bin"), env.get("PATH", "")])
    failing = []
    for n, part in enumerate(split_chain(s["script"])):
        m = re.match(r"^(?:tsx|node)\s+(\S+)$", part)
        junit = None
        cmd = part
        if re.match(r"^vitest\s+run\b", part):
            junit = os.path.join(tmp, f"{len(results)}-{n}.xml")
            cmd = f"{part} --reporter=default --reporter=junit --outputFile.junit={shlex.quote(junit)}"
        code, out = run(cmd, pkg_dir, log, env=env, shell=True)
        if code == 0:
            continue
        if junit and os.path.exists(junit):
            ids = junit_failures(junit)
            failing += ids or [part]
        else:
            failing.append(m.group(1) if m else part)
        if code is None:
            failing[-1] += " <timeout>"
    res["failing_tests"] = sorted(set(failing))
    res["status"] = "fail" if failing else "pass"
    res["seconds"] = round(time.time() - start, 1)
    results.append(res)
    say(f"[{res['status']}] {s['suite']} ({res['seconds']}s) {len(failing)} failing")

for s in uv_suites:
    d = s["dir"]
    log = log_for("uv-" + d)
    junit = os.path.join(tmp, re.sub(r"\W+", "_", d) + ".xml")
    start = time.time()
    res = {"suite": d, "dir": d, "kind": "uv", "runner": "uv", "status": "pass",
           "failing_tests": [], "seconds": 0}
    cmd = ["uv", "run", "--frozen", "--directory", os.path.join(root, d), "pytest", "-q",
           "-p", "no:cacheprovider", f"--junitxml={junit}"]
    code, out = run(cmd, os.path.join(root, d), log)
    res["seconds"] = round(time.time() - start, 1)
    if code is None:
        res.update(status="fail", failing_tests=["<timeout>"])
    elif not os.path.exists(junit):
        res.update(status="not-runnable", reason=first_line(
            "\n".join(l for l in out.splitlines() if re.search(r"error|Error|failed|not found", l)) or out))
    elif code == 0:
        pass
    elif code == 5:
        res.update(status="not-runnable", reason="pytest collected no tests (exit 5)")
    else:
        ids = junit_failures(junit)
        res.update(status="fail", failing_tests=ids or [f"<pytest exit {code}>"])
    results.append(res)
    say(f"[{res['status']}] {d} ({res['seconds']}s) {len(res['failing_tests'])} failing")

results.sort(key=lambda r: (r["kind"], r["suite"]))
os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
with open(out_path, "w") as fh:
    json.dump(results, fh, indent=2)
    fh.write("\n")
counts = {}
for r in results:
    counts[r["status"]] = counts.get(r["status"], 0) + 1
say(f"wrote {len(results)} results to {out_path}: {counts}")
PY
