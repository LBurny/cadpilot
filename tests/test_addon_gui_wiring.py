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
