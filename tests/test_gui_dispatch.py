"""Tests for the addon's GUI-thread dispatch guard (no FreeCAD needed).

``gui_dispatch`` imports FreeCAD / FreeCADGui / PySide at module level, so
this test stubs those modules in ``sys.modules`` and loads the addon file
directly by path. The stubs expose mutable input state (mouse buttons,
cursor position, window activity) so tests can simulate:

- a phantom/stuck button state (static buttons + cursor — must NOT starve
  the queue forever, even while the FreeCAD window is active);
- a real navigation drag (cursor moves every tick — tasks must defer);
- buttons held while another window is active (tasks must run).
"""

from __future__ import annotations

import importlib.util
import sys
import time
import types
from pathlib import Path

import pytest

ADDON = (
    Path(__file__).resolve().parent.parent / "addon" / "CADPilot" / "rpc_server" / "gui_dispatch.py"
)
# gui_dispatch does ``from rpc_server import dbglog``; without the addon dir on the
# path this file only ever worked inside a full-suite run (some later test module
# happened to insert it first), and failed with ModuleNotFoundError on its own.
if str(ADDON.parent.parent) not in sys.path:
    sys.path.insert(0, str(ADDON.parent.parent))


class _FakeSignal:
    def __init__(self):
        self._slots = []

    def connect(self, slot, _type=None):
        self._slots.append(slot)

    def emit(self):
        for s in self._slots:
            s()


class _FakeQObject:
    def __init__(self, *a, **k):
        pass


class _InputState:
    """Mutable input/window state backing the Qt stubs."""

    def __init__(self):
        self.buttons = 0  # Qt.NoButton
        self.cursor = (100, 100)
        self.window_active = True
        self.popup = None
        self.modal = None
        self.timer_callbacks = []


class _FakeStatusBar:
    def showMessage(self, _msg):
        pass

    def clearMessage(self):
        pass


class _FakeMainWindow:
    def __init__(self, state):
        self._state = state

    def isActiveWindow(self):
        return self._state.window_active

    def statusBar(self):
        return _FakeStatusBar()


class _FakeCursorPos:
    def __init__(self, xy):
        self._x, self._y = xy

    def x(self):
        return self._x

    def y(self):
        return self._y


@pytest.fixture()
def dispatch(monkeypatch):
    state = _InputState()

    qtcore = types.ModuleType("PySide.QtCore")
    qtcore.Qt = types.SimpleNamespace(NoButton=0, LeftButton=1, QueuedConnection=0)
    qtcore.QObject = _FakeQObject
    qtcore.Signal = _FakeSignal
    qtcore.QEventLoop = types.SimpleNamespace(ExcludeUserInputEvents=1, ExcludeSocketNotifiers=2)
    qtcore.QTimer = types.SimpleNamespace(
        singleShot=lambda _ms, cb: state.timer_callbacks.append(cb)
    )
    qtcore.QThread = types.SimpleNamespace(msleep=lambda _ms: None)

    qtwidgets = types.ModuleType("PySide.QtWidgets")
    qtwidgets.QApplication = types.SimpleNamespace(
        mouseButtons=lambda: state.buttons,
        activePopupWidget=lambda: state.popup,
        activeModalWidget=lambda: state.modal,
        instance=lambda: None,
    )

    qtgui = types.ModuleType("PySide.QtGui")
    qtgui.QCursor = types.SimpleNamespace(pos=lambda: _FakeCursorPos(state.cursor))

    pyside = types.ModuleType("PySide")
    pyside.QtCore = qtcore
    pyside.QtWidgets = qtwidgets
    pyside.QtGui = qtgui

    freecad = types.ModuleType("FreeCAD")
    freecad.Console = types.SimpleNamespace(PrintError=lambda _msg: None)

    freecadgui = types.ModuleType("FreeCADGui")
    freecadgui.getMainWindow = lambda: _FakeMainWindow(state)
    freecadgui.updateGui = lambda: None

    for name, mod in {
        "PySide": pyside,
        "PySide.QtCore": qtcore,
        "PySide.QtWidgets": qtwidgets,
        "PySide.QtGui": qtgui,
        "FreeCAD": freecad,
        "FreeCADGui": freecadgui,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    spec = importlib.util.spec_from_file_location("gui_dispatch_under_test", ADDON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Tests drive the heuristic (non-win32) guard paths by default; the win32
    # ground-truth path is exercised by overriding this per-test.
    mod._physical_buttons_down = lambda: None
    return mod, state


def _run_ticks(mod, n):
    for _ in range(n):
        mod.process_gui_tasks(reschedule=False)


def test_no_button_runs_immediately(dispatch):
    mod, _state = dispatch
    ran = []
    mod._rpc_request_queue.put(lambda: ran.append(1))
    _run_ticks(mod, 1)
    assert ran == [1]


def test_phantom_button_does_not_starve_queue(dispatch):
    """Stuck buttons + static cursor + active window = phantom state.

    The guard defers while the static-state cap has not elapsed, then treats
    the held button as phantom and runs the queue even though Qt still
    reports it held. The cap is time-based (guard evaluations are no longer
    ~500 ms heartbeat ticks), so the test shortens it instead of counting
    ticks.
    """
    mod, state = dispatch
    mod._PHANTOM_STATIC_SECONDS = 0.2  # production: 10 s
    state.buttons = 1  # LeftButton, stuck
    state.window_active = True
    ran = []
    mod._rpc_request_queue.put(lambda: ran.append(1))

    _run_ticks(mod, 3)
    assert ran == [], "phantom state should still defer before the cap elapses"

    time.sleep(0.25)
    _run_ticks(mod, 1)
    assert ran == [1], "static input state must be treated as phantom"


def test_real_drag_defers_until_release(dispatch):
    """A moving cursor with a held button is a real drag: never interrupt it."""
    mod, state = dispatch
    state.buttons = 1
    state.window_active = True
    ran = []
    mod._rpc_request_queue.put(lambda: ran.append(1))

    for i in range(15):
        state.cursor = (100 + i, 100 + i)  # cursor moves every tick
        _run_ticks(mod, 1)
    assert ran == [], "task ran during a real drag"

    state.buttons = 0  # released
    _run_ticks(mod, 1)
    assert ran == [1]


def test_button_held_in_inactive_window_runs(dispatch):
    mod, state = dispatch
    state.buttons = 1
    state.window_active = False  # dragging in some other app
    ran = []
    mod._rpc_request_queue.put(lambda: ran.append(1))
    _run_ticks(mod, 1)
    assert ran == [1]


# --- win32 OS ground-truth path ------------------------------------------------
#
# Qt's mouseButtons() is event-delivered state: stale exactly while the event
# loop is busy draining tasks. The OS physical state must be authoritative in
# BOTH directions, or a press that lands mid-task stays invisible until the
# backlog has run (the "frozen while the LLM models" race).


def test_physical_press_defers_even_when_qt_sees_no_button(dispatch):
    """Fresh-press race: the OS says the button is down but Qt has not
    delivered the press event yet — the queue must defer, not start mid-drag."""
    mod, state = dispatch
    mod._physical_buttons_down = lambda: 1
    state.buttons = 0  # Qt still stale (press event not delivered)
    state.window_active = True
    ran = []
    mod._rpc_request_queue.put(lambda: ran.append(1))
    _run_ticks(mod, 3)
    assert ran == [], "OS-visible press must defer the queue even when Qt is stale"


def test_physical_release_runs_even_when_qt_still_reports_held(dispatch):
    """Release side of the race: OS says the button is up, Qt still shows the
    stale hold — the queue must not keep deferring."""
    mod, state = dispatch
    mod._physical_buttons_down = lambda: 0
    state.buttons = 1  # stale Qt hold
    state.window_active = True
    ran = []
    mod._rpc_request_queue.put(lambda: ran.append(1))
    _run_ticks(mod, 1)
    assert ran == [1]


def test_real_physical_hold_is_never_capped(dispatch):
    """A genuine (OS-confirmed) hold must not hit the phantom caps: long
    inspection drags are normal, and capping punched a task through mid-drag."""
    mod, state = dispatch
    mod._physical_buttons_down = lambda: 1
    state.buttons = 1
    state.window_active = True
    mod._PHANTOM_STATIC_SECONDS = 0.05
    mod._PHANTOM_HOLD_SECONDS = 0.05
    ran = []
    mod._rpc_request_queue.put(lambda: ran.append(1))
    time.sleep(0.1)  # well past both phantom caps
    _run_ticks(mod, 2)
    assert ran == [], "phantom caps must not apply to an OS-confirmed real hold"


def test_press_mid_drain_pauses_remaining_backlog(dispatch):
    """A press that begins after a drain started must pause the remaining
    queue after the current task — not starve user input for the backlog."""
    mod, state = dispatch
    mod._physical_buttons_down = lambda: 0
    state.window_active = True
    ran = []

    def first():
        ran.append(1)
        # the user grabs the mouse while the first task is running
        mod._physical_buttons_down = lambda: 1
        state.buttons = 1

    mod._rpc_request_queue.put(first)
    mod._rpc_request_queue.put(lambda: ran.append(2))
    _run_ticks(mod, 1)  # one drain: task 1 runs, then the guard pauses the rest
    assert ran == [1], "the backlog must pause when the user presses mid-drain"

    mod._physical_buttons_down = lambda: 0  # released
    state.buttons = 0
    _run_ticks(mod, 1)
    assert ran == [1, 2], "the next tick resumes the drain"


def test_modal_dialog_defers(dispatch):
    mod, state = dispatch
    state.modal = object()
    ran = []
    mod._rpc_request_queue.put(lambda: ran.append(1))
    _run_ticks(mod, 3)
    assert ran == []
    state.modal = None
    _run_ticks(mod, 1)
    assert ran == [1]


# --- timeout diagnosis -------------------------------------------------------
#
# The timeout path used to blame the waker unconditionally:
#   "GUI dispatch timed out after 90s with an idle GUI thread (queue depth 1)
#    — the waker/heartbeat chain may be dead"
# which is a *guess*, and the wrong one while a human has a dialog open: the
# heartbeat is fine, the queue simply must not be drained mid-interaction.
# The user's own report — "it happens when I operate FreeCAD at the same time
# as the model, and the process CPU barely moves" — is that case, not a dead
# waker. Name the real cause; keep the dead-waker verdict for when it is true.


def _timeout_error(mod, state, guard):
    """Defer a few ticks under ``guard``, then let a call time out."""
    guard(state)
    mod._rpc_request_queue.put(lambda: None)  # something to defer for
    _run_ticks(mod, 3)
    return mod.dispatch_to_gui(lambda: None, timeout=0.02)


def test_timeout_names_an_open_modal_dialog(dispatch):
    mod, state = dispatch
    err = _timeout_error(mod, state, lambda s: setattr(s, "modal", object()))
    assert err["success"] is False
    assert "modal dialog" in err["error"], err["error"]
    assert "waker" not in err["error"], "the waker is fine; do not blame it"


def test_timeout_names_an_open_popup_menu(dispatch):
    mod, state = dispatch
    err = _timeout_error(mod, state, lambda s: setattr(s, "popup", object()))
    assert err["success"] is False
    assert "popup" in err["error"], err["error"]
    assert "waker" not in err["error"]


def test_timeout_names_a_real_drag(dispatch):
    mod, state = dispatch

    def drag(s):
        s.buttons = 1
        s.window_active = True

    err = _timeout_error(mod, state, drag)
    assert err["success"] is False
    assert "mouse button" in err["error"], err["error"]
    assert "waker" not in err["error"]


def test_timeout_still_blames_the_waker_when_nothing_defers(dispatch):
    """The genuine dead-chain case must keep its verdict — the fix is to stop
    mislabelling the dialog case, not to remove the diagnosis."""
    mod, _state = dispatch
    err = mod.dispatch_to_gui(lambda: None, timeout=0.02)
    assert err["success"] is False
    assert "waker" in err["error"], err["error"]


def test_long_deferral_is_logged_once(dispatch):
    """get_addon_log has to show why the queue stalled, without a warning per
    500 ms tick."""
    mod, state = dispatch
    warnings = []
    mod.logger = types.SimpleNamespace(
        warning=lambda *a, **k: warnings.append((a, k)),
        error=lambda *a, **k: None,
        info=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    state.modal = object()
    mod._rpc_request_queue.put(lambda: None)
    mod._DEFER_WARN_SECONDS = 0.0  # warn on the second identical tick
    _run_ticks(mod, 4)
    assert len(warnings) == 1, warnings
    assert "modal" in str(warnings[0]), warnings[0]
