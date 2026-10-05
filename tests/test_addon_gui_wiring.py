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
import contextlib
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


def test_step_column_is_user_resizable():
    """The Step column was Stretch — a label like 'execute_code: +1 object(s)…'
    elided with no way to widen it. Sections must be Interactive (or
    ResizeToContents), never Stretch."""
    modes = [
        _dotted(call.args[-1])
        for call in _calls(_PANEL)
        if (_dotted(call.func) or "").endswith(".setSectionResizeMode") and call.args
    ]
    assert modes, "the tree header must set an explicit resize mode"
    assert "QtWidgets.QHeaderView.Stretch" not in modes
    assert "QtWidgets.QHeaderView.Interactive" in modes


def test_column_width_is_persisted():
    """A resized Step column must survive a panel rebuild (settings key)."""
    source = (_ADDON / "rpc_server" / "step_panel.py").read_text(encoding="utf-8")
    assert "step_panel_step_width" in source


def test_double_click_focuses_the_editor():
    """Describing moved to selection; double-click keeps its edit affordance —
    it jumps into the parameter editor."""
    for call in _calls(_PANEL):
        if (_dotted(call.func) or "").endswith("itemDoubleClicked.connect"):
            assert call.args and _dotted(call.args[0]) == "self._focus_editor"
            return
    raise AssertionError("itemDoubleClicked is not connected")


def _panel_function(name: str):
    return next(n for n in ast.walk(_PANEL) if isinstance(n, ast.FunctionDef) and n.name == name)


def test_detail_card_does_not_repeat_the_description():
    """The description shows ONCE — in the log spotlight. The detail card's
    meta line repeating it read as noise."""
    func = _panel_function("_update_details")
    called = {_dotted(c.func) for c in ast.walk(func) if isinstance(c, ast.Call)}
    assert "sj.step_description" not in called


def test_selecting_a_step_shows_its_description_immediately():
    """One click is enough: selecting a step refreshes the description
    spotlight (double-click used to be the trigger)."""
    func = _panel_function("_on_selection")
    called = {_dotted(c.func) for c in ast.walk(func) if isinstance(c, ast.Call)}
    assert "self._update_spotlight" in called


def test_refresh_keeps_the_spotlight_in_sync():
    """The 1s refresh rebuilds the tree with signals blocked, so _on_selection
    does not fire — _render must refresh the spotlight itself (else a snippet's
    edited comment would not show until the selection moved)."""
    func = _panel_function("_render")
    called = {_dotted(c.func) for c in ast.walk(func) if isinstance(c, ast.Call)}
    assert "self._update_spotlight" in called


def test_spotlight_is_human_only_not_a_log_entry():
    """The spotlight is for the human, not the debug history: _update_spotlight
    must NOT append a log entry (the log is for debugging) — it sets ONE
    spotlight line rendered below the entries, timestamp included, replaced on
    the next selection."""
    func = _panel_function("_update_spotlight")
    called = {_dotted(c.func) for c in ast.walk(func) if isinstance(c, ast.Call)}
    assert "self._log" not in called, "the spotlight must not pollute the debug log"
    stores = [
        n
        for n in ast.walk(func)
        if isinstance(n, ast.Attribute) and n.attr == "_spotlight" and isinstance(n.ctx, ast.Store)
    ]
    assert stores, "_update_spotlight must set self._spotlight"


def test_detail_meta_elides_instead_of_wrapping():
    """The detail meta line is ONE elided line — a wrapped two-line meta read
    as clutter."""
    source = (_ADDON / "rpc_server" / "step_panel.py").read_text(encoding="utf-8")
    assert "detail_meta.setWordWrap(False)" in source
    assert "elidedText" in source


def test_param_editor_does_not_wrap():
    """The code pane must scroll horizontally instead of wrapping: a soft-
    wrapped code line is unreadable. NoWrap on the editor (the log console
    keeps WidgetWidth — prose can wrap, code cannot)."""
    for call in _calls(_PANEL):
        if not (_dotted(call.func) or "").endswith(".setLineWrapMode") or not call.args:
            continue
        if _dotted(call.args[0]) == "QtWidgets.QPlainTextEdit.NoWrap":
            return
    raise AssertionError("the parameter editor must set QPlainTextEdit.NoWrap")


def test_detail_meta_elides_against_the_padded_width():
    """Eliding against label.width() put the ellipsis under the 8px stylesheet
    padding — computed, but painted into the padding and clipped away. The
    elide target must be the contents rect (padding excluded)."""
    func = _panel_function("_set_meta")
    called = {_dotted(c.func) for c in ast.walk(func) if isinstance(c, ast.Call)}
    assert any((c or "").endswith(".contentsRect") for c in called)


def test_detail_meta_does_not_demand_the_text_width():
    """A non-wrapping QLabel reports minimumSizeHint == the full text width, so
    the layout took it as a width FLOOR — and every refresh wrote a
    differently-elided text, so the dock's width tracked the text and jittered.
    The horizontal size policy must be Ignored: the layout gives the label the
    available width and the elision fits inside it."""
    for call in _calls(_PANEL):
        if not (_dotted(call.func) or "").endswith(".setSizePolicy") or len(call.args) < 2:
            continue
        if _dotted(call.args[0]) == "QtWidgets.QSizePolicy.Ignored":
            return
    raise AssertionError("detail_meta must set an Ignored horizontal size policy")


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


# --- RPC watchdog + hot-restart wiring ---------------------------------------


def _module_consts(tree):
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            with contextlib.suppress(ValueError):
                consts[node.targets[0].id] = ast.literal_eval(node.value)
    return consts


def test_watchdog_property_names_are_single_sourced():
    watchdog = ast.parse((_ADDON / "rpc_server" / "watchdog.py").read_text(encoding="utf-8"))
    consts = _module_consts(watchdog)
    assert consts.get("PROPERTY") == "CADPilot_RPC_Watchdog"
    assert consts.get("DESIRED_PROPERTY") == "CADPilot_RPC_Desired"
    assert consts.get("SUPPRESS_PROPERTY") == "CADPilot_RPC_SuppressUntil"

    # InitGui (the one file a hot reload never touches) installs the timer.
    initgui_calls = {_dotted(c.func) for c in _calls(_INITGUI)}
    assert "watchdog.ensure_started" in initgui_calls

    # The toolbar toggles record user intent through the same helpers, so a
    # deliberate stop sticks while a programmatic one is treated as transient.
    commands = ast.parse((_ADDON / "rpc_server" / "commands.py").read_text(encoding="utf-8"))
    command_calls = {_dotted(c.func) for c in _calls(commands)}
    assert "watchdog.set_desired" in command_calls


def _top_level_rpc_deps(tree):
    """Sibling rpc_server.* modules a file imports at TOP LEVEL.

    Lazy imports inside functions are exempt: they resolve the module object
    at call time and always see the reloaded code.
    """
    deps = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("rpc_server."):
                deps.add(node.module)
            elif node.module == "rpc_server":
                deps.update(f"rpc_server.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            deps.update(a.name for a in node.names if a.name.startswith("rpc_server."))
    return deps


def test_restart_reload_order_imports_every_dependency_first():
    # importlib.reload does not rebind `from X import name` in the modules
    # that imported X, so an importer must be reloaded AFTER its dependency.
    rpc_source = (_ADDON / "rpc_server" / "rpc_server.py").read_text(encoding="utf-8")
    order = _module_consts(ast.parse(rpc_source)).get("_RELOAD_ORDER")
    assert order, "_RELOAD_ORDER not found in rpc_server.py"
    # The main module is reloaded last by restart_rpc_server itself.
    assert "rpc_server.rpc_server" not in order

    index = {name: i for i, name in enumerate(order)}
    assert len(index) == len(order), "duplicate entries in _RELOAD_ORDER"

    for path in sorted((_ADDON / "rpc_server").glob("*.py")):
        mod = f"rpc_server.{path.stem}"
        if path.stem == "__init__" or mod == "rpc_server.rpc_server":
            continue
        for dep in _top_level_rpc_deps(ast.parse(path.read_text(encoding="utf-8"))):
            assert dep in index, f"{mod} imports {dep}; add it to _RELOAD_ORDER"
            if mod in index:
                assert index[dep] < index[mod], (
                    f"{mod} is reloaded before its dependency {dep}; move "
                    f"{dep} first in _RELOAD_ORDER or its from-imports keep "
                    f"the OLD code alive after a hot restart"
                )
