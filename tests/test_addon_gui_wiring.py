"""Addon GUI wiring — guards two failure modes that are silent at runtime.

Both bugs they pin produced the same symptom ("clicking the toolbar button does
nothing") with no exception anywhere:

* PySide6 keeps only a weak reference to a bound method of a plain (non-QObject)
  Python object, so ``signal.connect(handler.Activated)`` goes dead as soon as
  the handler is garbage collected.
* ``findChild(StepPanel, name)`` matches on the Python class object, so after a
  hot reload every surviving dock belongs to the old class, is not found, and a
  duplicate dock is stacked on top of it.

The addon half has no Qt/FreeCAD test harness (it needs a live FreeCAD), so these
parse the source with ``ast`` — comments and docstrings, which legitimately spell
the bad patterns out, must not count, in the same spirit as the docstring-budget
test.
"""

import ast
from pathlib import Path

_ADDON = Path(__file__).resolve().parents[1] / "addon" / "CADPilot"
_INITGUI = ast.parse((_ADDON / "InitGui.py").read_text(encoding="utf-8"))
_PANEL = ast.parse((_ADDON / "rpc_server" / "step_panel.py").read_text(encoding="utf-8"))


def _dotted(node) -> str | None:
    """``a.b.c`` for an Attribute/Name chain, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _calls(tree):
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _connect_args(tree):
    return [
        arg
        for call in _calls(tree)
        if (_dotted(call.func) or "").endswith(".connect")
        for arg in call.args
    ]


def test_checkable_actions_do_not_connect_a_bare_bound_method():
    """``connect(handler.Activated)`` is collected by the GC and never fires."""
    offending = [
        arg
        for arg in _connect_args(_INITGUI)
        if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name)
    ]
    assert not offending, "bind the handler in a closure instead: lambda ..., h=handler: ..."


def test_checkable_actions_bind_their_handler_in_a_closure():
    """At least one connection must be a lambda carrying the handler."""
    lambdas = [a for a in _connect_args(_INITGUI) if isinstance(a, ast.Lambda)]
    assert lambdas
    bound = [d.id for lam in lambdas for d in lam.args.defaults if isinstance(d, ast.Name)]
    assert "handler" in bound


def test_panel_lookup_is_by_object_name_not_python_class():
    """Class-based findChild misses docks built by a pre-reload class."""
    for call in _calls(_PANEL):
        if _dotted(call.func) != "findChild" or not call.args:
            continue
        first = call.args[0]
        assert not (isinstance(first, ast.Name) and first.id == "StepPanel"), (
            "look docks up by objectName via findChildren"
        )


def test_panel_lifecycle_drops_duplicate_docks():
    """ensure_panel must collapse duplicates, not just create when absent."""
    lifecycle = next(
        n for n in ast.walk(_PANEL) if isinstance(n, ast.FunctionDef) and n.name == "ensure_panel"
    )
    called = {_dotted(c.func) for c in ast.walk(lifecycle) if isinstance(c, ast.Call)}
    assert "_drop_panel" in called


# --- FreeCAD's bare-exec namespace trap -------------------------------------
#
# FreeCAD runs InitGui.py with a bare exec() into a namespace that is NOT the
# __globals__ of the functions the file defines. A module-level name is
# therefore invisible inside _bootstrap()'s nested helpers, which only see
# their enclosing function's closures plus builtins. Getting this wrong is
# silent and total: find_addon_dir() raised NameError on `contextlib`, the
# bootstrap died, and the addon simply never loaded (no workbench, no RPC
# server, stale log). Verified live on FreeCAD 1.1.4.


def _own_scope(body):
    """Nodes belonging to one scope: never descends into a nested scope.

    A nested ``def``/``class``/``lambda`` is kept as a binding (it names
    something in this scope) but its body — and its signature, which belongs
    to neither — is left to the scope that owns it.
    """
    defs = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
    out: list = [n for n in body if isinstance(n, defs)]
    stack = [n for n in body if not isinstance(n, defs)]
    while stack:
        node = stack.pop()
        out.append(node)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, defs):
                out.append(child)
                continue
            stack.append(child)
    return out


def _bound_names(nodes) -> set[str]:
    """Names bound by these (already scope-limited) nodes."""
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            names |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names |= {a.asname or a.name for a in node.names if a.name != "*"}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
    return names


def _scope(func) -> tuple[list[str], list]:
    """(parameter names, body statements) for a function or lambda."""
    if isinstance(func, ast.Lambda):
        return [a.arg for a in func.args.args], [func.body]
    args = func.args
    params = [
        a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs] if isinstance(a, ast.arg)
    ]
    return params, func.body


def _module_level_names(body) -> set[str]:
    """Every name bound at InitGui module level — invisible to its functions."""
    return _bound_names(_own_scope(body))


def _violations(func, outer_env: set[str], module_names: set[str]) -> list[str]:
    """Module-level names read inside ``func`` without a closure/param/local binding."""
    params, body = _scope(func)
    scoped = _own_scope(body)
    env = outer_env | set(params) | _bound_names(scoped)
    label = getattr(func, "name", "<lambda>")
    bad = [
        f"{label}(): {n.id}"
        for n in scoped
        if isinstance(n, ast.Name)
        and isinstance(n.ctx, ast.Load)
        and n.id in module_names
        and n.id not in env
    ]
    for node in scoped:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            bad += _violations(node, env, module_names)
    return bad


def test_bootstrap_helpers_never_read_module_level_names():
    boot = next(
        n for n in _INITGUI.body if isinstance(n, ast.FunctionDef) and n.name == "_bootstrap"
    )
    module_names = _module_level_names(_INITGUI.body)
    # The module body itself (the _ADDON_HINT probe, the final except) may use
    # them — only the functions reachable from _bootstrap are constrained.
    offenders = _violations(boot, set(), module_names)
    assert not offenders, (
        "bare-exec(): import these inside _bootstrap() instead — "
        f"module-level names are not visible here: {sorted(set(offenders))}"
    )
