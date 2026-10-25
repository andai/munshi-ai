#!/usr/bin/env python3
"""images.py — build, check, ensure and push the munshi image catalogue (andai/images.json).

Run by andai/Makefile (`python3 -I andai/scripts/images.py ...`); stdlib only.

  build  --version V --platform P [--only a,b]   build every catalogue entry in order, tagged
                                                  ghcr.io/sunydepalpur/munshi-<name>:V and :dev
  check  --version V [--allow-test-images] CONFIG.json...
                                                  every compose image (and the runtime's spawn-time
                                                  image variables) is a catalogue image at V or on
                                                  the third_party allowlist; every FROM in a
                                                  catalogue Dockerfile is a catalogue image or an
                                                  official base image
  ensure --version V --platform P [--pull]        reads a rendered compose config on stdin and builds
                                                  (or, with --pull, pulls) the catalogue images it
                                                  needs that are not on this machine
  push   --version V --platform P                 pushes every catalogue tag for V (refuses without a
                                                  ghcr.io login or if a local image is not P)
  names                                           catalogue names, in build order

Rendered compose configs can contain secrets, so `ensure` reads them from stdin and never writes
them anywhere.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CATALOGUE = ROOT / "andai" / "images.json"
REGISTRY = "ghcr.io/sunydepalpur"
PREFIX = "munshi-"
# Image refs the compose files let a service override at spawn time (runtime environment).
SPAWN_VARS = ("BROWSER_IMAGE", "AGENT_IMAGE", "AGENT_WORKER_IMAGE")
# Test-only aliases, accepted by `check` only with --allow-test-images (the CI/test override).
TEST_IMAGES = ("mock-bot:dev",)
# Official base images a catalogue Dockerfile may build FROM (repository name, tag stripped).
OFFICIAL_BASES = (
    "python", "node", "debian", "ubuntu", "postgres", "valkey/valkey", "nginx",
    "versity/versitygw", "mcr.microsoft.com/playwright", "nvidia/cuda",
)


def load() -> dict:
    return json.loads(CATALOGUE.read_text())


def ref(name: str, version: str) -> str:
    return f"{REGISTRY}/{PREFIX}{name}:{version}"


def git_sha() -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def build_args(entry: dict, version: str) -> dict:
    values = {"registry": REGISTRY, "version": version, "git_sha": git_sha()}
    return {k: v.format(**values) for k, v in entry.get("args", {}).items()}


def deps(entry: dict, by_name: dict, version: str) -> list[str]:
    """Catalogue entries this entry is built FROM (via a build arg naming a catalogue image)."""
    out = []
    for value in build_args(entry, version).values():
        for name in by_name:
            if value == ref(name, version):
                out.append(name)
    if entry["name"] == "agent-worker-hardened":  # FROM .../munshi-agent-worker:${VERSION}
        out.append("agent-worker")
    return out


def tags(entry: dict, version: str) -> list[str]:
    return [ref(entry["name"], version), ref(entry["name"], "dev"), *entry.get("extra_tags", [])]


def docker(*args: str, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], text=True, check=check,
                          capture_output=capture)


def local(image: str) -> dict | None:
    p = docker("image", "inspect", image, capture=True, check=False)
    if p.returncode != 0:
        return None
    return json.loads(p.stdout)[0]


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.2f} {unit}"
        n /= 1000
    return f"{n:.2f} GB"


def select(cat: dict, only: str | None) -> list[dict]:
    entries = cat["images"]
    if not only:
        return entries
    wanted = {s.strip() for s in only.split(",") if s.strip()}
    unknown = wanted - {e["name"] for e in entries}
    if unknown:
        sys.exit(f"images: not in the catalogue: {', '.join(sorted(unknown))}")
    return [e for e in entries if e["name"] in wanted]


def cmd_build(a) -> int:
    cat = load()
    entries = select(cat, a.only)
    rows = []
    for i, e in enumerate(entries, 1):
        name = e["name"]
        cmd = ["buildx", "build", "--platform", a.platform, "--load",
               "-f", str(ROOT / e["dockerfile"])]
        if a.builder:
            cmd += ["--builder", a.builder]
        for t in tags(e, a.version):
            cmd += ["-t", t]
        for k, v in build_args(e, a.version).items():
            cmd += ["--build-arg", f"{k}={v}"]
        cmd.append(str(ROOT / e["context"]))
        print(f"\n==> [{i}/{len(entries)}] {ref(name, a.version)}  ({e['dockerfile']})", flush=True)
        start = time.monotonic()
        rc = subprocess.run(["docker", *cmd]).returncode
        secs = time.monotonic() - start
        if rc != 0:
            print(f"\nimages: BUILD FAILED: {name} (exit {rc}) after {secs:.0f}s", file=sys.stderr)
            return rc
        info = local(ref(name, a.version)) or {}
        arch = f"{info.get('Os')}/{info.get('Architecture')}"
        rows.append((name, secs, info.get("Size", 0), arch, info.get("Id", "")[7:19]))
        print(f"==> built {ref(name, a.version)}  {human(info.get('Size', 0))}  {arch}  {secs:.0f}s",
              flush=True)
    print("\nimage                     platform      size        build   id")
    for name, secs, size, arch, iid in rows:
        print(f"{name:<25} {arch:<13} {human(size):>10}  {secs:>5.0f}s  {iid}")
    total = sum(r[1] for r in rows)
    print(f"{len(rows)} images, {total / 60:.1f} min, {human(sum(r[2] for r in rows))} (layers shared)")
    return 0


def stack_refs(config: dict) -> list[tuple[str, str]]:
    """(where, image) for every service image and spawn-time image variable in a rendered config."""
    out = []
    for svc, body in sorted(config.get("services", {}).items()):
        if body.get("image"):
            out.append((f"service {svc}", body["image"]))
        env = body.get("environment") or {}
        if isinstance(env, list):
            env = dict(x.split("=", 1) for x in env if "=" in x)
        for var in SPAWN_VARS:
            if env.get(var):
                out.append((f"service {svc} env {var}", env[var]))
    return out


def dockerfile_froms(path: Path, args: dict) -> list[str]:
    """FROM image refs of a Dockerfile, ARGs resolved, stage aliases dropped."""
    text = re.sub(r"\\\n", " ", path.read_text())
    defaults: dict[str, str] = {}
    stages: set[str] = set()
    froms = []
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"(?i)^ARG\s+([A-Za-z_][A-Za-z0-9_]*)(?:=(\S*))?", line)
        if m and not froms:  # global ARGs (before the first FROM) are usable in FROM
            defaults[m.group(1)] = m.group(2) or ""
            continue
        m = re.match(r"(?i)^FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", line)
        if not m:
            continue
        image = re.sub(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?",
                       lambda v: args.get(v.group(1), defaults.get(v.group(1), "")), m.group(1))
        if image.lower() not in stages:
            froms.append(image)
        if m.group(2):
            stages.add(m.group(2).lower())
    # A FROM that names an earlier stage via ARG (e.g. deps-${RUNTIME_DEPS}) resolves to a stage.
    return [f for f in froms if f.lower() not in stages]


def official(image: str) -> bool:
    repo = re.sub(r"@sha256:.*$", "", image)
    repo = re.sub(r":[^/:]+$", "", repo)
    if repo.startswith("docker.io/"):
        repo = repo[len("docker.io/"):]
    if repo.startswith("library/"):
        repo = repo[len("library/"):]
    return repo in OFFICIAL_BASES


def cmd_check(a) -> int:
    cat = load()
    names = [e["name"] for e in cat["images"]]
    catalogue_refs = {ref(n, a.version): n for n in names}
    allowed_third = set(cat["third_party"])
    test_ok = set(TEST_IMAGES) if a.allow_test_images else set()
    problems, seen = [], []
    for path in a.configs:
        config = json.loads(Path(path).read_text())
        for where, image in stack_refs(config):
            label = f"{Path(path).name}: {where}"
            if image in catalogue_refs:
                seen.append((label, image, "catalogue"))
            elif image in allowed_third:
                seen.append((label, image, "third-party (allowlisted)"))
            elif image in test_ok:
                seen.append((label, image, "test override"))
            else:
                hint = " (test image; only allowed with ALLOW_TEST_IMAGES=1)" if image in TEST_IMAGES else ""
                problems.append(f"{label}: {image} is neither a catalogue image at {a.version} "
                                f"nor on the third_party allowlist{hint}")
    bases = []
    for e in cat["images"]:
        df = ROOT / e["dockerfile"]
        if not df.is_file():
            problems.append(f"{e['name']}: Dockerfile {e['dockerfile']} not found")
            continue
        if not (ROOT / e["context"]).is_dir():
            problems.append(f"{e['name']}: context {e['context']} not found")
        for image in dockerfile_froms(df, build_args(e, a.version)):
            if image in catalogue_refs and names.index(catalogue_refs[image]) < names.index(e["name"]):
                bases.append((e["name"], image, "catalogue (built earlier)"))
            elif official(image):
                bases.append((e["name"], image, "official base"))
            else:
                problems.append(f"{e['dockerfile']}: FROM {image} is neither an earlier catalogue "
                                "image nor an official base image")
    if not a.quiet:
        print(f"Stack image references ({len(seen)}):")
        for label, image, kind in seen:
            print(f"  {kind:<26} {image:<70} {label}")
        print(f"Dockerfile bases ({len(bases)}):")
        for name, image, kind in bases:
            print(f"  {kind:<26} {image:<70} {name}")
    if problems:
        print(f"images-check: {len(problems)} problem(s):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    return 0


def cmd_ensure(a) -> int:
    cat = load()
    by_name = {e["name"]: e for e in cat["images"]}
    by_tag = {}
    for e in cat["images"]:
        for t in tags(e, a.version):
            by_tag[t] = e["name"]
    config = json.load(sys.stdin)
    needed, unknown = [], []
    for _, image in stack_refs(config):
        if image in by_tag:
            if by_tag[image] not in needed:
                needed.append(by_tag[image])
        elif image not in cat["third_party"] and not local(image):
            unknown.append(image)
    if unknown:
        print("images: not in the catalogue and not on this machine: " + ", ".join(unknown),
              file=sys.stderr)
        return 1
    if a.pull:
        for name in needed:
            print(f"docker pull {ref(name, a.version)}", flush=True)
            if docker("pull", "--platform", a.platform, ref(name, a.version), check=False).returncode:
                return 1
            for t in tags(by_name[name], a.version)[1:]:
                docker("tag", ref(name, a.version), t)
        return 0
    # Build what is missing, plus missing catalogue bases those builds need, in catalogue order.
    todo: set[str] = set()

    def want(name: str) -> None:
        todo.add(name)
        for d in deps(by_name[name], by_name, a.version):
            if d not in todo and not local(ref(d, a.version)):
                want(d)

    referenced = {image for _, image in stack_refs(config)}
    for name in needed:
        required = {ref(name, a.version)} | (set(tags(by_name[name], a.version)) & referenced)
        if any(local(t) is None for t in required):
            want(name)
    if not todo:
        print(f"images: all {len(needed)} catalogue images the stack needs are on this machine")
        return 0
    order = [e["name"] for e in cat["images"] if e["name"] in todo]
    print("images: missing locally, building: " + ", ".join(order), flush=True)
    return cmd_build(argparse.Namespace(version=a.version, platform=a.platform,
                                        only=",".join(order), builder=a.builder))


def ghcr_login() -> bool:
    cfg_dir = Path(os.environ.get("DOCKER_CONFIG") or Path.home() / ".docker")
    try:
        cfg = json.loads((cfg_dir / "config.json").read_text())
    except (OSError, ValueError):
        return False
    hosts = set(cfg.get("auths", {})) | set(cfg.get("credHelpers", {}))
    return any(h in hosts for h in ("ghcr.io", "https://ghcr.io"))


def cmd_push(a) -> int:
    cat = load()
    if not ghcr_login():
        print("images-push: no ghcr.io login in the docker config. Authenticate first:\n"
              "  gh auth refresh -h github.com -s write:packages\n"
              "  gh auth token | docker login ghcr.io -u sunydepalpur --password-stdin",
              file=sys.stderr)
        return 1
    missing, wrong = [], []
    for e in cat["images"]:
        info = local(ref(e["name"], a.version))
        if info is None:
            missing.append(e["name"])
        elif f"{info['Os']}/{info['Architecture']}" != a.platform:
            wrong.append(f"{e['name']} ({info['Os']}/{info['Architecture']})")
    if missing or wrong:
        if missing:
            print("images-push: not built: " + ", ".join(missing) + " (run make -C andai images)",
                  file=sys.stderr)
        if wrong:
            print(f"images-push: not {a.platform}: " + ", ".join(wrong), file=sys.stderr)
        return 1
    for e in cat["images"]:
        image = ref(e["name"], a.version)
        print(f"\n==> docker push {image}", flush=True)
        if docker("push", image, check=False).returncode:
            print(f"images-push: push failed: {image}", file=sys.stderr)
            return 1
    print(f"\npushed {len(cat['images'])} images at {a.version}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("build", "ensure", "push"):
        s = sub.add_parser(name)
        s.add_argument("--version", required=True)
        s.add_argument("--platform", default="linux/amd64")
        s.add_argument("--builder", default=os.environ.get("BUILDX_BUILDER") or "")
        if name == "build":
            s.add_argument("--only", default="")
        if name == "ensure":
            s.add_argument("--pull", action="store_true")
    s = sub.add_parser("check")
    s.add_argument("--version", required=True)
    s.add_argument("--allow-test-images", action="store_true")
    s.add_argument("--quiet", action="store_true")
    s.add_argument("configs", nargs="+")
    sub.add_parser("names")
    a = p.parse_args()
    if a.cmd == "names":
        print(" ".join(e["name"] for e in load()["images"]))
        return 0
    return {"build": cmd_build, "check": cmd_check, "ensure": cmd_ensure, "push": cmd_push}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
