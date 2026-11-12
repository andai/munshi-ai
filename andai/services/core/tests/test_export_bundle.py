"""The data export bundle format (contract `export-bundle.v1`): the committed JSON Schema is the one
the pydantic models generate, the goldens validate against it (and against the models), and a field
that is not part of the format (a secret, another person's address) cannot be written."""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from andai_core.dpdp import bundle

CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "export-bundle.v1"
GOLDEN = CONTRACT / "golden"
MODELS = {"manifest": bundle.Manifest, "account": bundle.Account, "meeting": bundle.MeetingExport}


def test_the_committed_schema_is_the_models_schema() -> None:
    committed = (CONTRACT / "export-bundle.schema.json").read_text(encoding="utf-8")
    assert committed == bundle.schema_text(), (
        "regenerate with `uv run --directory andai python -m andai_core.dpdp.bundle --schema`"
    )
    assert set(json.loads(committed)["$defs"]) >= {"Manifest", "Account", "MeetingExport"}


@pytest.mark.parametrize("path", sorted(GOLDEN.glob("*.json")), ids=lambda p: p.name)
def test_every_golden_is_a_valid_model_and_round_trips(path: Path) -> None:
    model = MODELS[path.name.split("-")[0]]
    data = json.loads(path.read_text(encoding="utf-8"))
    parsed = model.model_validate(data)
    assert json.loads(bundle.dump(parsed)) == data


def test_the_goldens_validate_with_the_contract_validator() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [node, str(CONTRACT / "validate.mjs"), "--check"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _account(**extra) -> dict:
    return {
        "userId": str(uuid.uuid4()),
        "email": "asha@example.test",
        "displayName": "Asha",
        "createdAt": datetime.now(UTC).isoformat(),
        "lastLoginAt": None,
        "twoFactorEnabled": False,
        "membership": {"role": "member", "joinedAt": datetime.now(UTC).isoformat(), "teams": []},
        "processing": {"note": "note"},
        **extra,
    }


@pytest.mark.parametrize(
    "field",
    ["passwordHash", "totpSecret", "secretCiphertext", "recoveryCodes", "sessionToken", "apiKey"],
)
def test_secrets_have_no_place_in_the_account_file(field: str) -> None:
    bundle.Account.model_validate(_account())
    with pytest.raises(ValidationError):
        bundle.Account.model_validate(_account(**{field: "x"}))


def test_other_people_appear_by_name_only() -> None:
    meeting = json.loads((GOLDEN / "meeting-recorded-hinglish.json").read_text(encoding="utf-8"))
    bundle.MeetingExport.model_validate(meeting)
    grant = {"type": "user", "name": "R", "email": "r@example.test"}
    with_email = {**meeting, "sharedWith": [grant]}
    with pytest.raises(ValidationError):
        bundle.MeetingExport.model_validate(with_email)
    with pytest.raises(ValidationError):
        bundle.MeetingExport.model_validate({**meeting, "attendeeEmails": ["r@example.test"]})
    consent = {**meeting["consent"], "removedByEmail": "r@example.test"}
    with pytest.raises(ValidationError):
        bundle.MeetingExport.model_validate({**meeting, "consent": consent})


def test_a_manifest_names_its_format_and_checksums() -> None:
    manifest = json.loads((GOLDEN / "manifest-self-service.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == bundle.SCHEMA
    with pytest.raises(ValidationError):
        bundle.Manifest.model_validate({**manifest, "schema": "munshi.export-bundle/v2"})
    bad = [{**manifest["files"][0], "sha256": "not-a-checksum"}]
    with pytest.raises(ValidationError):
        bundle.Manifest.model_validate({**manifest, "files": bad})
