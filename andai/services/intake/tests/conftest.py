"""Fixtures for andai-intake tests: andai-core's test fixtures and helpers (its throwaway Postgres,
initialised with db-init.sql and migrated to head, and the rest), re-exported so pytest registers
them for this directory. Without Docker they skip, and `ANDAI_REQUIRE_DOCKER=1` turns the skip into
a failure.

andai-core's `services/core/tests/conftest.py` is loaded under the module name
`andai_core_test_support` (shared with andai-worker's tests). This directory is not a package:
andai-worker's tests already are the package `tests`, and a second one would collide. As a plain
directory, this module is imported as the top-level `conftest` and replaces andai-core's in
`sys.modules` when both directories are collected; it therefore re-exports every public name of
andai-core's conftest, so andai-core's tests that import from `conftest` inside a test still get
the same helpers.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_NAME = "andai_core_test_support"
if _NAME in sys.modules:
    _support = sys.modules[_NAME]
else:
    _spec = importlib.util.spec_from_file_location(
        _NAME, Path(__file__).resolve().parents[2] / "core" / "tests" / "conftest.py"
    )
    assert _spec is not None and _spec.loader is not None
    _support = importlib.util.module_from_spec(_spec)
    sys.modules[_NAME] = _support
    _spec.loader.exec_module(_support)

globals().update(
    {name: value for name, value in vars(_support).items() if not name.startswith("_")}
)
