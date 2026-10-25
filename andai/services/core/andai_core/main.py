"""andai-core service skeleton.

Phase 1 exposes only a health endpoint; product behaviour lands in later phases.
"""

import os

from fastapi import FastAPI

SERVICE_NAME = "andai-core"

app = FastAPI(title=SERVICE_NAME, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {
        "service": SERVICE_NAME,
        "status": "ok",
        "version": os.environ.get("ANDAI_VERSION", "dev"),
    }
