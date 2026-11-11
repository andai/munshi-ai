"""Every audit action andai writes is registered (`andai_core.audit_actions`) and documented.

The scan reads every `audit.record(...)` call in `andai/services/*/andai_*`. Its action argument
must be one of:

* a registered string literal, or a conditional expression whose branches are;
* a name assigned in the enclosing function, or at module level, only from such values;
* the `action` parameter of a listed wrapper (`WRAPPERS`), whose own call sites are then checked
  by the same rules.

Anything else (an f-string, a concatenation, a call, another function's parameter, an attribute)
cannot be checked statically and fails the scan, so a call site cannot build action strings at run
time. `audit.record` also refuses an unregistered action when it runs.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy import Engine

from andai_core import audit
from andai_core.audit_actions import ACTIONS, CATEGORIES, markdown
from andai_core.db.session import transaction
from conftest import bootstrap_org, new_user

SERVICES = Path(__file__).resolve().parents[2]
DOCS = SERVICES.parent / "docs" / "audit-actions.md"
RECORD_ACTION_INDEX = 3  # record(tx, org_id, actor_user_id, action, target, ip)


@dataclass(frozen=True)
class Wrapper:
    """A function that passes its `action` parameter on to `audit.record`."""

    module: str  # path relative to andai/services
    name: str
    position: int | None  # positional index of `action` at call sites; None: keyword-only
    attribute_of: str  # the module name other modules call it through ("" when private)


WRAPPERS = (
    Wrapper("core/andai_core/auth/mfa.py", "record", None, "mfa"),
    Wrapper("core/andai_core/auth/resets.py", "_audit_in_every_org", 2, ""),
)


@dataclass
class Scan:
    used: dict[str, list[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    wrapper_calls: dict[str, int] = field(default_factory=dict)


class _Unresolved(Exception):
    pass


def _functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _enclosing(tree: ast.Module, node: ast.AST):
    """The innermost function containing `node`, or None at module level."""
    best = None
    for func in _functions(tree):
        if any(child is node for child in ast.walk(func)):
            if best is None or any(child is func for child in ast.walk(best)):
                best = func
    return best


def _params(func) -> set[str]:
    a = func.args
    return {p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)} | {
        p.arg for p in (a.vararg, a.kwarg) if p is not None
    }


def _assigned(scope: list[ast.stmt] | ast.AST, name: str, *, nested: bool) -> list[ast.expr]:
    nodes = ast.walk(scope) if nested else iter(scope)
    values = []
    for node in nodes:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                values.append(node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name) and node.target.id == name:
                values.append(node.value)
    return values


def _literals(expr: ast.expr, tree: ast.Module, func, wrapper: Wrapper | None) -> list[str]:
    """The action strings `expr` can hold; [] when it is a wrapper's own `action` parameter."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return [expr.value]
    if isinstance(expr, ast.IfExp):
        return _literals(expr.body, tree, func, wrapper) + _literals(
            expr.orelse, tree, func, wrapper
        )
    if isinstance(expr, ast.Name):
        if func is not None:
            if wrapper is not None and expr.id == "action" and "action" in _params(func):
                return []
            if expr.id in _params(func):
                raise _Unresolved(f"parameter {expr.id!r} of {func.name}() is not a listed wrapper")
            local = _assigned(func, expr.id, nested=True)
            if local:
                return [s for value in local for s in _literals(value, tree, func, wrapper)]
        module = _assigned(tree.body, expr.id, nested=False)
        if module:
            return [s for value in module for s in _literals(value, tree, None, None)]
        raise _Unresolved(f"name {expr.id!r} has no literal assignment")
    raise _Unresolved(f"{type(expr).__name__} cannot be checked statically")


def _action_arg(call: ast.Call, position: int | None) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == "action":
            return keyword.value
    if position is not None and len(call.args) > position:
        return call.args[position]
    return None


def _is_audit_record(call: ast.Call) -> bool:
    f = call.func
    return isinstance(f, ast.Attribute) and f.attr == "record" and (
        isinstance(f.value, ast.Name) and f.value.id == "audit"
    )


def _wrapper_called(call: ast.Call, module: str, wrapper: Wrapper) -> bool:
    f = call.func
    if isinstance(f, ast.Name):
        return module == wrapper.module and f.id == wrapper.name
    return (
        bool(wrapper.attribute_of)
        and isinstance(f, ast.Attribute)
        and f.attr == wrapper.name
        and isinstance(f.value, ast.Name)
        and f.value.id == wrapper.attribute_of
    )


def _check_imports(tree: ast.Module, module: str, scan: Scan) -> None:
    """`audit.record` and the wrappers are only reachable under the names the scan knows."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        names = {(a.name, a.asname) for a in node.names}
        if node.module == "andai_core.audit" or (
            node.module == "andai_core.auth.mfa" and any(n == "record" for n, _ in names)
        ):
            scan.errors.append(f"{module}:{node.lineno}: import the module, not record()")
        if node.module in ("andai_core", "andai_core.auth"):
            for name, asname in names:
                if name in ("audit", "mfa") and asname not in (None, name):
                    scan.errors.append(f"{module}:{node.lineno}: {name} imported as {asname}")


def scan_sources(sources: dict[str, str]) -> Scan:
    """Scan modules given as {path relative to andai/services: source}."""
    scan = Scan(wrapper_calls={w.name: 0 for w in WRAPPERS})
    for module, source in sorted(sources.items()):
        tree = ast.parse(source, filename=module)
        _check_imports(tree, module, scan)
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            sites: list[tuple[int | None, Wrapper | None]] = []
            if _is_audit_record(call):
                sites.append((RECORD_ACTION_INDEX, None))
            for wrapper in WRAPPERS:
                if _wrapper_called(call, module, wrapper):
                    scan.wrapper_calls[wrapper.name] += 1
                    sites.append((wrapper.position, wrapper))
            for position, called in sites:
                where = f"{module}:{call.lineno}"
                arg = _action_arg(call, position)
                if arg is None:
                    scan.errors.append(f"{where}: no action argument")
                    continue
                func = _enclosing(tree, call)
                inside = next(
                    (
                        w
                        for w in WRAPPERS
                        if called is None and func is not None
                        and w.module == module and w.name == func.name
                    ),
                    None,
                )
                try:
                    actions = _literals(arg, tree, func, inside)
                except _Unresolved as exc:
                    scan.errors.append(f"{where}: {exc}")
                    continue
                for action in actions:
                    if action not in ACTIONS:
                        scan.errors.append(f"{where}: unregistered action {action!r}")
                    scan.used.setdefault(action, []).append(where)
    return scan


def _service_sources() -> dict[str, str]:
    return {
        str(path.relative_to(SERVICES)): path.read_text()
        for path in sorted(SERVICES.glob("*/andai_*/**/*.py"))
    }


# ─── the code ─────────────────────────────────────────────────────────────────────────────────────


def test_every_written_action_is_registered() -> None:
    scan = scan_sources(_service_sources())
    assert scan.errors == []
    assert set(scan.used) <= set(ACTIONS)


def test_every_registered_action_is_written_somewhere() -> None:
    scan = scan_sources(_service_sources())
    assert set(ACTIONS) - set(scan.used) == set()


def test_every_wrapper_exists_and_is_called() -> None:
    sources = _service_sources()
    for wrapper in WRAPPERS:
        tree = ast.parse(sources[wrapper.module])
        names = {f.name for f in tree.body if isinstance(f, ast.FunctionDef)}
        assert wrapper.name in names, wrapper
    assert all(count > 0 for count in scan_sources(sources).wrapper_calls.values())


# ─── the scan itself ──────────────────────────────────────────────────────────────────────────────

GOOD = '''
from andai_core import audit
ACTION = "share.grant"

def literal(tx):
    audit.record(tx, o, a, "share.revoke", t)

def constant(tx):
    audit.record(tx, o, a, ACTION, t)

def local(tx, flag):
    action = "org.require_2fa.enable" if flag else "org.require_2fa.disable"
    audit.record(tx, o, a, action, t)

def keyword(tx):
    audit.record(tx, o, a, action="mom.edit", target=t)
'''


def test_scan_accepts_literals_constants_locals_and_keywords() -> None:
    scan = scan_sources({"core/andai_core/x.py": GOOD})
    assert scan.errors == []
    assert set(scan.used) == {
        "share.grant",
        "share.revoke",
        "org.require_2fa.enable",
        "org.require_2fa.disable",
        "mom.edit",
    }


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ('audit.record(tx, o, a, f"share.{kind}", t)', "JoinedStr"),
        ('audit.record(tx, o, a, "share." + kind, t)', "BinOp"),
        ("audit.record(tx, o, a, kind.lower(), t)", "Call"),
        ("audit.record(tx, o, a, settings.ACTION, t)", "Attribute"),
        ("audit.record(tx, o, a, kind, t)", "parameter 'kind'"),
        ('audit.record(tx, o, a, "share.granted", t)', "unregistered action 'share.granted'"),
        ('audit.record(tx, o, a, "share.grant" if kind else "nope", t)', "unregistered"),
        ("audit.record(tx, o, a)", "no action argument"),
    ],
)
def test_scan_refuses_what_it_cannot_check(body: str, error: str) -> None:
    source = f"from andai_core import audit\n\ndef f(tx, kind):\n    {body}\n"
    scan = scan_sources({"core/andai_core/x.py": source})
    assert len(scan.errors) == 1 and error in scan.errors[0], scan.errors


def test_scan_follows_wrappers_to_their_call_sites() -> None:
    mfa = (
        "from andai_core import audit\n\n"
        "def record(tx, *, org_id, actor, action, user_id, ip):\n"
        '    audit.record(tx, org_id, actor, action, f"user:{user_id}", ip)\n\n'
        "def lockout(tx):\n"
        '    record(tx, org_id=o, actor=a, action="mfa.lockout", user_id=u, ip=None)\n'
    )
    caller = (
        "from andai_core.auth import mfa\n\n"
        "def route(tx, kind):\n"
        '    mfa.record(tx, org_id=o, actor=a, action="mfa.made_up", user_id=u, ip=None)\n'
        "    mfa.record(tx, org_id=o, actor=a, action=kind, user_id=u, ip=None)\n"
    )
    scan = scan_sources(
        {"core/andai_core/auth/mfa.py": mfa, "core/andai_core/auth/routes.py": caller}
    )
    assert scan.used.keys() == {"mfa.lockout", "mfa.made_up"}
    assert len(scan.errors) == 2
    assert "unregistered action 'mfa.made_up'" in scan.errors[0]
    assert "parameter 'kind'" in scan.errors[1]


@pytest.mark.parametrize(
    "line",
    [
        "from andai_core.audit import record",
        "from andai_core import audit as trail",
        "from andai_core.auth.mfa import record",
    ],
)
def test_scan_refuses_aliases_it_would_not_see(line: str) -> None:
    scan = scan_sources({"core/andai_core/x.py": line + "\n"})
    assert len(scan.errors) == 1


# ─── the registry, the docs and record() ──────────────────────────────────────────────────────────


def test_registry_entries_are_well_formed() -> None:
    for key, entry in ACTIONS.items():
        assert key == key.strip().lower() and "." in key and " " not in key, key
        assert entry.category in CATEGORIES, key
        assert entry.label.strip() == entry.label and entry.label, key


def test_docs_list_exactly_the_registry() -> None:
    assert DOCS.read_text() == markdown()


def test_markdown_command_prints_the_docs_table() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "andai_core.audit_actions", "--markdown"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out == markdown()


def test_record_refuses_an_unregistered_action(app_engine: Engine) -> None:
    org_id = bootstrap_org(app_engine, new_user(app_engine))
    with transaction(app_engine, org_id=org_id) as tx:
        with pytest.raises(ValueError, match="unregistered audit action 'share.granted'"):
            audit.record(tx, org_id, None, "share.granted", f"meeting:{uuid.uuid4()}")
