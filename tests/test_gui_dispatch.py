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
import types
from pathlib import Path

import pytest

ADDON = (
    Path(__file__).resolve().parent.parent
    / "addon" / "CADPilot" / "rpc_server" / "gui_dispatch.py"
)


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
    mod._PHANTOM_TICK_LIMIT = 5  # keep tests fast; production value is 20
    return mod, state


def _run_ticks(mod, n):
    for _ in range(n):
        mod.process_gui_tasks(reschedule=False)


def test_no_button_runs_immediately(dispatch):
    mod, state = dispatch
    ran = []
    mod._rpc_request_queue.put(lambda: ran.append(1))
    _run_ticks(mod, 1)
    assert ran == [1]


def test_phantom_button_does_not_starve_queue(dispatch):
    """Stuck buttons + static cursor + active window = phantom state.

    The guard may defer a few ticks, but after the tick limit the queued
    task must run even though Qt still reports the button held.
    """
    mod, state = dispatch
    state.buttons = 1  # LeftButton, stuck
    state.window_active = True
    ran = []
    mod._rpc_request_queue.put(lambda: ran.append(1))

    _run_ticks(mod, mod._PHANTOM_TICK_LIMIT)
    assert ran == [], "phantom state should still defer the first ticks"

    _run_ticks(mod, 3)
    assert ran == [1], "static input state must be treated as phantom"


def test_real_drag_defers_until_release(dispatch):
    """A moving cursor with a held button is a real drag: never interrupt it."""
    mod, state = dispatch
    state.buttons = 1
    state.window_active = True
    ran = []
    mod._rpc_request_queue.put(lambda: ran.append(1))

    for i in range(mod._PHANTOM_TICK_LIMIT * 3):
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
