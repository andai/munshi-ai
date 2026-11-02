"""Live tools for the minutes language model.

    python -m andai_core.llm probe --env-file /abs/path/.env --out /abs/path/llm-probe.txt

`probe` makes one minutes call (`LlmClient.chat_json` with the draft schema, its one repair retry
included) on a tiny synthetic transcript against the route in the env file (`load_live_settings`)
and writes whether the reply was schema-valid, the model id, the latency and the token counts. It
never prints or writes the key or the host. Only synthetic text is sent (D-21).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from andai_core.llm.client import LlmClient, LlmError, load_live_settings
from andai_core.minutes.chunking import Segment, line
from andai_core.minutes.generate import Draft
from andai_core.minutes.prompt import messages
from andai_core.minutes.templates import TEMPLATES

SYNTHETIC = (
    Segment("seg_probe_0001", 3.0, "Asha", "Chaliye shuru karte hain, aaj budget review hai."),
    Segment(
        "seg_probe_0002",
        11.0,
        "Ravi",
        "Vendor ka quote 4 lakh aaya hai, hum use approve karte hain.",
    ),
    Segment("seg_probe_0003", 19.0, "Asha", "Theek hai. Ravi, final sheet kal tak bhej dena."),
    Segment(
        "seg_probe_0004",
        26.0,
        "Ravi",
        "हाँ, मैं कल तक भेज दूँगा। Delivery late ho sakti hai, woh risk hai.",
    ),
)


async def _probe(env_file: Path) -> list[str]:
    config = load_live_settings(env_file)
    client = LlmClient(config)
    report = [
        f"checked_at: {datetime.now(UTC).isoformat(timespec='seconds')}",
        f"model: {config.model}",
    ]
    try:
        result = await client.chat_json(
            messages(
                [line(s, str(n)) for n, s in enumerate(SYNTHETIC, start=1)],
                attendees=["Asha", "Ravi"],
                template=TEMPLATES["standard"],
                language="hinglish",
                meeting_date=date(2026, 11, 2),
            ),
            Draft,
            name="minutes_probe",
        )
    except LlmError as exc:
        return [*report, "schema_valid: false", f"error: {exc.code}"]
    draft = Draft.model_validate(result.data)
    known = {str(n) for n in range(1, len(SYNTHETIC) + 1)}  # the line numbers the prompt shows
    cited = [
        e
        for section in (draft.decisions, draft.action_items, draft.topics, draft.risks)
        for item in section
        for e in item.evidence
    ]
    return [
        *report,
        "schema_valid: true",
        f"reported_model: {result.model}",
        f"latency_s: {result.usage.latency_s:.2f}",
        f"calls: {result.usage.calls}",
        f"prompt_tokens: {result.usage.prompt_tokens}",
        f"completion_tokens: {result.usage.completion_tokens}",
        "items: "
        + ", ".join(
            f"{name}={len(section)}"
            for name, section in (
                ("decisions", draft.decisions),
                ("action_items", draft.action_items),
                ("topics", draft.topics),
                ("risks", draft.risks),
            )
        ),
        f"evidence_ids_cited: {len(cited)}",
        f"evidence_ids_unknown: {sum(1 for e in cited if e not in known)}",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m andai_core.llm")
    sub = parser.add_subparsers(dest="command", required=True)
    probe = sub.add_parser("probe", help="one schema-constrained call on synthetic text")
    probe.add_argument("--env-file", type=Path, required=True)
    probe.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    lines = asyncio.run(_probe(args.env_file))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    valid = "schema_valid: true" in lines
    print(f"schema_valid: {'true' if valid else 'false'} (written to {args.out.name})")
    return 0 if valid else 1


if __name__ == "__main__":
    sys.exit(main())
