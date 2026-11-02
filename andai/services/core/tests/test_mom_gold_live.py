"""The minutes gold set through the configured LLM route (MOM-02 SC3, SC2 latency; D-21, D-45,
D-47).

Run with `-m live` and `ENV_FILE=<absolute path of a local env file>` that names the route
(`ANDAI_LLM_*`, or the stack's `MUNSHI_LLM_*`, read by `llm.client.load_live_settings`). Only the 40
committed synthetic fixtures are sent. The test fails (it does not skip) when `ENV_FILE` is set but
names no route.

The report is written first, to `.work/phases/06-mom-pipeline/live/gold-<date>T<time>.json` and
`.md` (or `GOLD_REPORT_DIR`); it never names the route's host or key. The SC3 thresholds are
computed into its `sc3` block as pass or fail per criterion. This test asserts only the run's
integrity: all 40 processed and the report written by this run. A route that fails schema output
on more than 10% of calls stops the run, which then fails here with the report saved.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path

import pytest

from andai_core.llm.client import LlmClient, LlmNotConfigured, load_live_settings
from andai_core.minutes import eval as gold

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parent / "mom_gold"
REPO = Path(__file__).resolve().parents[4]
DEFAULT_OUT = REPO / ".work" / "phases" / "06-mom-pipeline" / "live"


def test_the_gold_set_through_the_live_route() -> None:
    env_file = os.environ.get("ENV_FILE", "").strip()
    if not env_file:
        pytest.skip("ENV_FILE is not set")
    try:
        config = load_live_settings(env_file)
    except (LlmNotConfigured, OSError) as exc:
        pytest.fail(f"ENV_FILE names no usable LLM route: {type(exc).__name__}")
    fixtures = gold.load_all(ROOT)
    assert len(fixtures) == 40
    out_dir = Path(os.environ.get("GOLD_REPORT_DIR") or DEFAULT_OUT)
    started = datetime.now().astimezone()
    stem = f"gold-{started:%Y-%m-%dT%H%M}"

    def progress(run: gold.MeetingRun) -> None:
        labels = run.score.labels if run.score is not None else []
        found = sum(1 for lr in labels if lr.found)
        print(
            f"{run.language}/{run.fixture}: {run.seconds:.1f} s, {run.parts} part(s), "
            f"{found}/{run.labels_total} labels{', error ' + run.error if run.error else ''}",
            flush=True,
        )

    began = time.time()
    runs, stats, stopped = asyncio.run(
        gold.run_all(
            LlmClient(config).chat_json,
            fixtures,
            model=config.model,
            context_tokens=config.context_tokens,
            max_output_tokens=config.max_output_tokens,
            on_meeting=progress,
        )
    )
    report = gold.report(runs, stats, expected=len(fixtures))
    report["run"] = {
        "started": started.isoformat(timespec="seconds"),
        "wall_s": round(time.time() - began, 1),
        "model": config.model,
        "context_tokens": config.context_tokens,
        "max_output_tokens": config.max_output_tokens,
        "template": "standard",
        "stopped_for_schema_failures": stopped,
        "claim_limit": (
            "40 synthetic scripted meetings, one route and model, one date; not real speech, "
            "not other models."
        ),
    }
    json_path, md_path = gold.write_report(report, out_dir, stem=stem)
    print(f"report: {json_path} and {md_path.name}")
    print(json.dumps(report["sc3"], ensure_ascii=False, indent=1))

    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert json_path.stat().st_mtime >= began
    assert not stopped, "the route failed schema output on more than 10% of calls: run stopped"
    assert saved["processed"] == 40 and "sc3" in saved
