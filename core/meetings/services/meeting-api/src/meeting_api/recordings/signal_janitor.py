"""The captured-signal tape budget — rolling eviction over the ``signal/`` roots (O-TEL-1).

Tapes live under the top-level ``signal/`` root, or under ``org/<uuid>/signal/`` for meetings with
a storage prefix. The budget is one budget across every root.

Fixture collection is DEFAULT ON, so the bound cannot live on the capture side: every prod meeting
tapes, and the only question is how much we keep. This is that answer — a keep-side budget with
rolling eviction. Newest in, oldest unpromoted out. Curation decides what survives (promote a tape
into the regression library and it is never evicted), not the capture path, which stays dumb.

Why this shape rather than a TTL: a TTL either deletes fixtures we still need or lets the bucket
grow past what we can pay for, depending on traffic. A byte budget is the quantity we actually care
about, and it holds regardless of how many meetings a week turns out to be.

Three properties that make it safe to run unattended:

  * **Promoted tapes are never evicted** — a ``PROMOTED`` marker object beside a tape spares it. If
    promotion alone exceeds the budget the sweep stops and says so LOUDLY rather than deleting the
    curated library to satisfy an arithmetic target. That is a human's call.
  * **A tape is evicted whole**, all its parts together. Half a tape (frames without the STT
    round-trips beside them) is not a smaller fixture, it is a broken one.
  * **In-flight uploads are out of scope** — anything younger than ``min_age_s`` is skipped, so a
    multi-part upload in progress can never be half-deleted by a sweep that fires mid-teardown.

Pure over the ``Storage`` port: no DB, no HTTP, no clock of its own beyond ``now``. The whole sweep
is one paginated listing plus one delete per evicted object.
"""
from __future__ import annotations

import time
from typing import Optional

from ..obs import log_event
from .jsonb import (
    ORG_STORAGE_ROOT,
    SIGNAL_PROMOTED_MARKER,
    SIGNAL_ROOT_PREFIX,
    STORAGE_PREFIX_RE,
)
from .ports import Storage

# 50 GB of tapes. The number is the deployment's storage bill, not a technical limit — it is env
# steered (SIGNAL_TAPE_BUDGET_BYTES) precisely because the right value is an operator's decision.
DEFAULT_BUDGET_BYTES = 50 * 1024 * 1024 * 1024
# Never touch a tape younger than this — the in-flight-upload guard.
DEFAULT_MIN_AGE_S = 600.0


def _tape_prefix(key: str) -> Optional[str]:
    """The owning tape prefix (``[org/<uuid>/]signal/{user}/{meeting}/{session}/``) for a key.

    Returns ``None`` for anything that is not shaped like a tape object, which is how a stray key
    under ``signal/`` gets LEFT ALONE rather than swept up by a delete loop that assumed everything
    it listed was ours.
    """
    parts = key.split("/")
    root = 0
    if len(parts) == 7 and STORAGE_PREFIX_RE.fullmatch("/".join(parts[:2])):
        root = 2  # org / <uuid> / signal / …
    # signal / user / meeting / session / <file>
    tape = parts[root:]
    if len(tape) != 5 or f"{tape[0]}/" != SIGNAL_ROOT_PREFIX or not tape[4]:
        return None
    return "/".join(parts[:root + 4]) + "/"


async def signal_tape_roots(storage: Storage) -> list[str]:
    """Every ``signal/`` root in the bucket: the top-level one and one per ``org/<uuid>/`` prefix.

    Org prefixes are found with a delimiter listing of ``org/``, which returns one entry per org and
    never pages through the recordings stored inside them.
    """
    roots = [SIGNAL_ROOT_PREFIX]
    for child in await storage.list_children(ORG_STORAGE_ROOT):
        if STORAGE_PREFIX_RE.fullmatch(child.rstrip("/")):
            roots.append(f"{child}{SIGNAL_ROOT_PREFIX}")
    return roots


def group_tapes(objects: list[dict]) -> dict[str, dict]:
    """Fold a flat object listing into one entry per tape.

    Each entry: ``{prefix, keys, bytes, newest, promoted}``. ``newest`` (not oldest) drives eviction
    order — a tape's parts land within seconds of each other, and using the newest object means a
    marker written later never makes a tape look older than it is.
    """
    tapes: dict[str, dict] = {}
    for obj in objects:
        key = obj["key"]
        prefix = _tape_prefix(key)
        if prefix is None:
            continue
        tape = tapes.setdefault(prefix, {
            "prefix": prefix, "keys": [], "bytes": 0, "newest": 0.0, "promoted": False,
        })
        tape["keys"].append(key)
        tape["bytes"] += int(obj.get("size") or 0)
        tape["newest"] = max(tape["newest"], float(obj.get("last_modified") or 0.0))
        if key.rsplit("/", 1)[-1] == SIGNAL_PROMOTED_MARKER:
            tape["promoted"] = True
    return tapes


async def sweep_signal_tapes(
    storage: Storage,
    *,
    budget_bytes: int = DEFAULT_BUDGET_BYTES,
    min_age_s: float = DEFAULT_MIN_AGE_S,
    now: Optional[float] = None,
    prefix: Optional[str] = None,
) -> dict:
    """Run ONE budget sweep. Returns counters; never raises for an ordinary storage hiccup.

    ``prefix`` limits the sweep to one root; by default it covers every root
    (``signal_tape_roots``) under one budget.

    Under budget → nothing is deleted and nothing is logged at operator level (a quiet sweep is the
    normal case, and a log line per tick would bury the one that matters).
    """
    now = time.time() if now is None else now
    roots = [prefix] if prefix is not None else await signal_tape_roots(storage)
    objects = [obj for root in roots for obj in await storage.list_detailed(root)]
    tapes = group_tapes(objects)
    total = sum(t["bytes"] for t in tapes.values())
    if total <= budget_bytes:
        return {"tapes": len(tapes), "bytes": total, "evicted": 0, "evicted_bytes": 0,
                "over_budget": False}

    # Oldest first, promoted spared, in-flight skipped.
    candidates = sorted(
        (t for t in tapes.values() if not t["promoted"] and (now - t["newest"]) >= min_age_s),
        key=lambda t: t["newest"],
    )
    evicted = 0
    evicted_bytes = 0
    for tape in candidates:
        if total <= budget_bytes:
            break
        failed = False
        for key in sorted(tape["keys"]):
            try:
                await storage.delete(key)
            except Exception as e:  # noqa: BLE001 — one unhappy object must not stall the sweep
                failed = True
                log_event(
                    "signal_tape_evict_failed", audience="operator", level="warning",
                    span="recordings.signal.janitor",
                    fields={"key": key, "error": str(e)},
                )
        if failed:
            # A partially-deleted tape is already unusable, but do NOT count bytes we may not have
            # freed — the next sweep re-reads the truth from the listing.
            continue
        total -= tape["bytes"]
        evicted += 1
        evicted_bytes += tape["bytes"]
        log_event(
            "signal_tape_evicted", audience="operator", span="recordings.signal.janitor",
            fields={"prefix": tape["prefix"], "bytes": tape["bytes"],
                    "parts": len(tape["keys"]), "age_s": round(now - tape["newest"], 1)},
        )

    if total > budget_bytes:
        # Everything left is either promoted or too young. Say so — this is the state where the
        # budget stops being self-enforcing and somebody has to decide (raise the budget, or
        # un-promote). Silently continuing to grow is the failure this whole module exists to stop.
        promoted_bytes = sum(t["bytes"] for t in tapes.values() if t["promoted"])
        log_event(
            "signal_tape_budget_unreclaimable", audience="operator", level="warning",
            span="recordings.signal.janitor",
            fields={"bytes": total, "budget_bytes": budget_bytes,
                    "promoted_bytes": promoted_bytes, "tapes": len(tapes),
                    "hint": "every remaining tape is promoted or younger than min_age_s — "
                            "raise SIGNAL_TAPE_BUDGET_BYTES or un-promote fixtures"},
        )
    else:
        log_event(
            "signal_tape_budget_swept", audience="operator", span="recordings.signal.janitor",
            fields={"evicted": evicted, "evicted_bytes": evicted_bytes,
                    "bytes": total, "budget_bytes": budget_bytes, "tapes": len(tapes) - evicted},
        )
    return {"tapes": len(tapes), "bytes": total, "evicted": evicted,
            "evicted_bytes": evicted_bytes, "over_budget": total > budget_bytes}
