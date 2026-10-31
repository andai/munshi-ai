"""Search under /api/v1 (AUTH_MATRIX row `/api/v1/search`).

* `GET /search?q=&limit=&cursor=` — the meetings the caller may read whose title, stored transcript
  or latest minutes match `q`, best first, with up to three snippets each and the total number of
  such meetings (`service.py`). Any member; results are access-filtered in SQL, so nobody sees or
  counts a meeting they may not open. `q` needs 2 to 200 characters with at least two letters or
  digits after folding (else 422 `bad_query`); `limit` is 1–50 (default 20); `cursor` is the
  previous page's `next_cursor` for the same `q` (else 422 `bad_cursor`).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from andai_core.auth.deps import Services, get_services
from andai_core.auth.roles import Caller, require_role
from andai_core.db.session import transaction
from andai_core.meetings.routes import EVERYONE, actor
from andai_core.search import service

router = APIRouter(prefix="/api/v1")


@router.get("/search")
def search(
    q: str = Query(max_length=service.MAX_QUERY_CHARS),
    limit: int = Query(default=service.DEFAULT_LIMIT, ge=1, le=service.MAX_LIMIT),
    cursor: str | None = Query(default=None, max_length=service.MAX_CURSOR_CHARS),
    caller: Caller = Depends(require_role(*EVERYONE)),
    services: Services = Depends(get_services),
) -> dict:
    who = actor(caller)
    with transaction(services.engine, org_id=who.org_id, user_id=who.user_id) as tx:
        return service.search(tx, who, q, limit=limit, cursor=cursor)
