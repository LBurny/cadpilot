"""Mouse-guard race regression pins — the "frozen while the LLM models" bug.

The addon half has no Qt/FreeCAD test harness (it needs a live FreeCAD), so
these parse ``gui_dispatch.py`` with ``ast`` — same spirit as
``test_addon_gui_wiring.py``.

Pinned root causes (every one let MCP tasks run while the user was actively
dragging the mouse in FreeCAD):

* Qt's ``mouseButtons()`` is event-delivered state — stale exactly while the
  event loop is busy draining tasks. The OS physical state was only used to
  *veto* phantom holds, never to *detect* fresh ones, so a press that began
  while a task ran stayed invisible and the next task started mid-drag. The
  physical check must be consulted first.
* An OS-confirmed (real) hold must never be cut off by the phantom caps:
  long inspection drags are normal CAD usage, and the old 15 s cap punched a
  queued task straight through one. The caps are heuristic-path-only now.
* The drain loop checked the interaction guards once, before its first task;
  a press that began mid-drain could not pause the backlog.
* The static-state phantom cap counted ~500 ms heartbeat ticks; guard
  evaluations are no longer that far apart (between-task rechecks), so the
  cap must measure time.
"""

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "addon" / "CADPilot" / "rpc_server" / "gui_dispatch.py"
_TREE = ast.parse(_SRC.read_text(encoding="utf-8"))


def _func(name: str) -> ast.FunctionDef:
    for node in ast.walk(_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in gui_dispatch.py")


def _calls(tree) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


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


def test_physical_button_state_is_checked_before_qt_state():
    """Fresh presses/releases are OS-real-time; Qt state lags the event loop."""
    fn = _func("_user_holding_button")
    physical = [c for c in _calls(fn) if _dotted(c.func) == "_physical_buttons_down"]
    qt = [c for c in _calls(fn) if (_dotted(c.func) or "").endswith("mouseButtons")]
    assert physical, "the guard must consult the OS physical button state"
    assert qt, "the heuristic fallback path must still read Qt mouseButtons()"
    assert min(c.lineno for c in physical) < min(c.lineno for c in qt), (
        "checking Qt first re-opens the stale-state race: a press that lands "
        "while a task runs is invisible until the event loop delivers it"
    )


def test_os_confirmed_holds_are_never_capped():
    """The phantom caps are heuristic-path only; a real hold is never cut off."""
    fn = _func("_user_holding_button")
    qt_line = min(c.lineno for c in _calls(fn) if (_dotted(c.func) or "").endswith("mouseButtons"))
    early_caps = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Name)
        and n.id in ("_PHANTOM_STATIC_SECONDS", "_PHANTOM_HOLD_SECONDS")
        and n.lineno < qt_line
    ]
    assert not early_caps, (
        "caps evaluated before the Qt fallback also apply to OS-confirmed real "
        "holds — that is the punch-through that froze users mid-drag"
    )


def test_drain_loop_rechecks_interaction_guards_between_tasks():
    """A press/dialog that appears mid-drain must pause the remaining backlog."""
    fn = _func("process_gui_tasks")
    loops = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.While)
        and any("empty" in (_dotted(c.func) or "") for c in _calls(n.test))
    ]
    assert loops, "drain loop not found"
    names = {_dotted(c.func) or "" for c in _calls(loops[0])}
    assert "_user_holding_button" in names, (
        "no mouse-guard recheck inside the drain loop: user input starves "
        "for the whole backlog once a drain has started"
    )
    assert any(n.endswith("activePopupWidget") for n in names)
    assert any(n.endswith("activeModalWidget") for n in names)


def test_inactive_window_resets_hold_trackers():
    """A stale _held_since must not survive an inactive window (false cap trip)."""
    fn = _func("_user_holding_button")
    resets = [c for c in _calls(fn) if _dotted(c.func) == "_reset_hold_trackers"]
    # FreeCADGui.getMainWindow().isActiveWindow() has a Call inside the chain,
    # so match the method name on the Attribute directly.
    active_checks = [c for c in _calls(fn) if getattr(c.func, "attr", None) == "isActiveWindow"]
    assert len(active_checks) >= 2, (
        "both the win32 and heuristic paths must scope to the active window"
    )
    assert len(resets) >= len(active_checks), (
        "every not-our-drag exit must reset the hold trackers, or the hold cap "
        "fires immediately when the user comes back mid-hold"
    )


def test_static_cap_is_time_based_not_tick_based():
    """Guard evaluations are no longer ~500 ms apart; tick counting lies."""
    names = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)}
    assert "_PHANTOM_STATIC_SECONDS" in names
    assert "_held_static_ticks" not in names
    assert "_PHANTOM_TICK_LIMIT" not in names
