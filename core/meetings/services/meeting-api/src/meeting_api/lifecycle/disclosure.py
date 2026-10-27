"""The bot's disclosure outcome (lifecycle.v1 ``disclosure``) → the block stored at
``meeting.data.disclosure``.

After admission the bot posts the recording notice in meeting chat and reports how that went on an
``active`` event: ``channel`` ``chat`` (with ``posted_at``) or ``bot_name_only`` (with ``detail``).
This module only normalises that report. Storing it is the repo's ``record_disclosure``, which
keeps the first report and ignores later ones.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

DISCLOSURE_CHANNELS = frozenset({"chat", "bot_name_only"})
_DETAIL_MAX_LEN = 500
_POSTED_AT_MAX_LEN = 64


def disclosure_outcome(event: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """The ``{channel, posted_at?, detail?}`` block from a lifecycle event, or ``None`` when the
    event carries no usable report. Unknown keys are dropped; strings are capped."""
    raw = event.get("disclosure") if isinstance(event, dict) else None
    if not isinstance(raw, dict):
        return None
    channel = raw.get("channel")
    if channel not in DISCLOSURE_CHANNELS:
        return None
    out: Dict[str, str] = {"channel": channel}
    posted_at = raw.get("posted_at")
    if isinstance(posted_at, str) and posted_at.strip():
        out["posted_at"] = posted_at.strip()[:_POSTED_AT_MAX_LEN]
    detail = raw.get("detail")
    if isinstance(detail, str) and detail.strip():
        out["detail"] = detail.strip()[:_DETAIL_MAX_LEN]
    return out
