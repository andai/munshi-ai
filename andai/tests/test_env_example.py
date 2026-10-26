"""andai/scripts/env_example.py keeps the root .env.example complete and its used-by lines true."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "andai" / "scripts" / "env_example.py"

spec = importlib.util.spec_from_file_location("env_example", SCRIPT)
env_example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(env_example)

STACK = {
    "name": "x",
    "services": {
        "api": {"environment": {"ZZ_ALPHA": "${ZZ_ALPHA:-1}", "ZZ_BETA": "${ZZ_BETA}"}, "image": "img:${ZZ_TAG:-dev}"},
        "web": {"ports": ["127.0.0.1:${ZZ_ALPHA}:80"]},
    },
    "volumes": {"data": {"name": "${COMPOSE_PROJECT_NAME}_data"}},
}


def run(tmp_path: Path, template: str, mode: str) -> subprocess.CompletedProcess[str]:
    stack = tmp_path / "stack.json"
    stack.write_text(json.dumps(STACK))
    env = tmp_path / ".env.example"
    env.write_text(template)
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--compose-json", str(stack), "--env-example", str(env), mode],
        capture_output=True, text=True,
    )


# Keys are named so no andai tooling file mentions them; tooling readers are tested separately.
CURRENT = """# used by: api, web
ZZ_ALPHA=1
# used by: api
#ZZ_BETA=
# used by: api
ZZ_TAG=0.1.0
# used by: docker compose (project name)
COMPOSE_PROJECT_NAME=x
"""


def test_current_template_passes(tmp_path):
    r = run(tmp_path, CURRENT, "--check")
    assert r.returncode == 0, r.stderr


def test_missing_variable_fails(tmp_path):
    r = run(tmp_path, CURRENT.replace("# used by: api\n#ZZ_BETA=\n", ""), "--check")
    assert r.returncode == 1
    assert "ZZ_BETA: read by api but missing" in r.stderr


def test_unused_key_fails(tmp_path):
    r = run(tmp_path, CURRENT + "# nobody reads this\nORPHAN=1\n", "--check")
    assert r.returncode == 1
    assert "ORPHAN: in .env.example but read by no service" in r.stderr


def test_stale_used_by_fails_and_write_fixes_it(tmp_path):
    stale = CURRENT.replace("# used by: api, web\n", "# used by: api\n")
    assert run(tmp_path, stale, "--check").returncode == 1
    assert run(tmp_path, stale, "--write").returncode == 0
    assert (tmp_path / ".env.example").read_text() == CURRENT


def test_write_adds_lines_to_a_bare_template(tmp_path):
    bare = "ZZ_ALPHA=1\n#ZZ_BETA=\nZZ_TAG=0.1.0\nCOMPOSE_PROJECT_NAME=x\n"
    assert run(tmp_path, bare, "--write").returncode == 0
    assert (tmp_path / ".env.example").read_text() == CURRENT


def test_tooling_reference_counts_as_a_reader():
    refs = env_example.tooling_refs({"ADMIN_TOKEN", "NOT_A_REAL_KEY_XYZ"})
    assert "ADMIN_TOKEN" in refs
    assert "NOT_A_REAL_KEY_XYZ" not in refs


def test_repo_template_has_no_secrets():
    values = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            values[k] = v
    for key in ("ADMIN_TOKEN", "INTERNAL_API_SECRET", "ANDAI_KEK", "ANDAI_DB_PASSWORD_OWNER", "ANDAI_DB_PASSWORD_APP"):
        assert values[key] == "", key
    assert values["TRANSCRIPTION_SERVICE_TOKEN"] == "__OLLM_API_KEY__"
