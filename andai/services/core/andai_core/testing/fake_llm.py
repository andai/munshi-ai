"""A deterministic, OpenAI-compatible chat route for tests and the e2e compose profile.

It speaks just enough of `POST /v1/chat/completions` for `andai_core.llm.LlmClient`: it reads the
minutes prompt andai sends (`minutes/prompt.py`) and answers one schema-valid JSON object built from
what it sees there, never from a model:

* **minutes / minutes_part** (a `Draft`): a summary; one decision, one topic and one risk citing
  line numbers from the transcript; one action item owned by the first attendee the prompt
  names (`ownerConfidence` 0.9, `dueText` "kal"), and one with no owner (`ownerConfidence` 0.2),
  which andai's owner rule turns into `unassigned`.
* **minutes_merge** (a `Merged`): a summary and one group per text that more than one partial item
  of a kind carries, naming those items' references.

Texts are in the output language the system message asks for: English, Hindi in Devanagari, or
Roman Hinglish. Nothing is logged.

`GET /healthz` answers for the compose health check. Run it with
`python -m andai_core.testing.fake_llm [--host 0.0.0.0] [--port 8000]`; the e2e compose profile
starts it as the `fake-llm` service. The default stack never does.
"""

from __future__ import annotations

import argparse
import json
import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from andai_core.minutes.templates import LANGUAGE_INSTRUCTION

SEGMENT_LINE = re.compile(r"^\[([^\]\s]+)\] ", re.MULTILINE)
ITEM_LINE = re.compile(r"^([DATR])(\d+\.\d+) (.*)$", re.MULTILINE)
ATTENDEES_LINE = re.compile(r"^Attendees: (.*)$", re.MULTILINE)
NOT_KNOWN = "(not known)"

TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "summary": "Summary: the meeting covered {n} points.",
        "decision": "Decision: go ahead with the plan as discussed.",
        "action_named": "Send the report",
        "action_unassigned": "Confirm the budget",
        "topic": "Topic: plan and budget",
        "risk": "Risk: the budget is still pending",
    },
    "hi": {
        "summary": "सारांश: बैठक में {n} बातें हुईं।",
        "decision": "निर्णय: तय योजना पर आगे बढ़ना है।",
        "action_named": "रिपोर्ट भेजनी है",
        "action_unassigned": "बजट की पुष्टि करनी है",
        "topic": "विषय: योजना और बजट",
        "risk": "जोखिम: बजट अभी लंबित है",
    },
    "hinglish": {
        "summary": "Saransh: meeting mein {n} baatein hui.",
        "decision": "Nirnay: plan ke saath aage badhna hai.",
        "action_named": "Report bhejni hai",
        "action_unassigned": "Budget confirm karna hai",
        "topic": "Vishay: plan aur budget",
        "risk": "Risk: budget abhi pending hai",
    },
}


def language_of(system: str) -> str:
    """The output language a system message asks for (English when it names none)."""
    for language, instruction in LANGUAGE_INSTRUCTION.items():
        if instruction in system:
            return language
    return "en"


def _first_attendee(user: str) -> str | None:
    found = ATTENDEES_LINE.search(user)
    if not found or found.group(1).strip() == NOT_KNOWN:
        return None
    names = [n.strip() for n in found.group(1).split(",") if n.strip()]
    return names[0] if names else None


def draft(system: str, user: str) -> dict[str, Any]:
    """A `Draft` (by alias) for one map or single call."""
    text = TEXTS[language_of(system)]
    ids = list(dict.fromkeys(SEGMENT_LINE.findall(user.split("Transcript:", 1)[-1])))
    if not ids:
        return {
            "summary": text["summary"].format(n=0),
            "decisions": [], "actionItems": [], "topics": [], "risks": [],
        }  # fmt: skip
    first, second, last = ids[0], ids[min(1, len(ids) - 1)], ids[-1]
    owner = _first_attendee(user)
    return {
        "summary": text["summary"].format(n=len(ids)),
        "decisions": [{"text": text["decision"], "evidence": [first]}],
        "actionItems": [
            {
                "text": text["action_named"],
                "owner": owner,
                "ownerConfidence": 0.9 if owner else 0.0,
                "dueText": "kal",
                "due": None,
                "evidence": [second],
            },
            {
                "text": text["action_unassigned"],
                "owner": None,
                "ownerConfidence": 0.2,
                "dueText": None,
                "due": None,
                "evidence": [last],
            },
        ],
        "topics": [{"title": text["topic"], "evidence": [first, second]}],
        "risks": [{"text": text["risk"], "evidence": [last]}],
    }


def merged(system: str, user: str) -> dict[str, Any]:
    """A `Merged` (by alias): items of one kind with the same text grouped by reference."""
    text = TEXTS[language_of(system)]
    body = user.split("Partial minutes:", 1)[-1]
    by_text: dict[tuple[str, str], list[str]] = {}
    for letter, number, item in ITEM_LINE.findall(body):
        by_text.setdefault((letter, item), []).append(f"{letter}{number}")
    groups = [{"sources": refs} for refs in by_text.values() if len(refs) > 1]
    count = sum(len(refs) for refs in by_text.values())
    return {"summary": text["summary"].format(n=count), "groups": groups}


def completion(body: dict) -> dict[str, Any]:
    messages = body.get("messages") or []
    system = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
    user = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
    name = (((body.get("response_format") or {}).get("json_schema") or {}).get("name")) or ""
    content = merged(system, user) if name == "minutes_merge" else draft(system, user)
    reply = json.dumps(content, ensure_ascii=False)
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": body.get("model") or "fake-llm",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": sum(len(str(m.get("content", ""))) for m in messages) // 4,
            "completion_tokens": len(reply) // 4,
        },
    }


def create_app() -> FastAPI:
    app = FastAPI(title="fake-llm", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"service": "fake-llm", "status": "ok"}

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": {"message": "the body is not JSON"}}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": {"message": "the body is not an object"}}, 400)
        return JSONResponse(completion(body))

    return app


app = create_app()


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    parser = argparse.ArgumentParser(prog="python -m andai_core.testing.fake_llm")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
