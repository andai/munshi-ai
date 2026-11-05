"""Signed test mail and a stub DNS zone for the sender-verification tests.

The kit lives in `andai_intake.testing.sign`, which the e2e stack's fake DNS also uses; this module
re-exports it under the name the tests import. `Kit()` holds one in-memory stub zone
(`dns.StubResolver`) and the keys it has published; keys are generated when the tests run and live
only in memory.
"""

from __future__ import annotations

from andai_intake.testing.sign import (
    NOTETAKER,
    SELECTOR,
    Key,
    Kit,
    append_to_body,
    message,
    replace_header,
    rsa_key,
)

__all__ = [
    "NOTETAKER",
    "SELECTOR",
    "Key",
    "Kit",
    "append_to_body",
    "message",
    "replace_header",
    "rsa_key",
]
