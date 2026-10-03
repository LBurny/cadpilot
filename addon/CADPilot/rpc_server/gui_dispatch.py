"""GUI-thread task dispatch for the RPC server.

The XML-RPC server runs in its own thread. FreeCAD APIs that touch the GUI
or the document tree must run in the main GUI thread. This module owns the
queue that ferries wrapped callables onto the GUI thread and the helper that
RPC handlers use to invoke them.

Robustness and performance guarantees:

1. Per-call response queues: each ``dispatch_to_gui`` call owns its own
   ``queue.Queue``. A timeout in one call can never corrupt the response for
   a subsequent call.
2. Immediate wake via Qt signal: ``dispatch_to_gui`` emits a signal from the
   RPC thread; the GUI thread processes the task immediately rather than
   waiting for the next 500 ms heartbeat tick. The 500 ms heartbeat is kept
   only as a fallback.
3. Mouse-button guard: ``process_gui_tasks`` skips the current tick while
   mouse buttons are held so MCP tasks cannot interrupt 3D navigation drags.
   Phantom/stuck button states are filtered out by three independent checks
   (OS physical button state, static-tick cap, hold-duration cap) so they
   can never starve the queue — see ``_user_holding_button``.
4. Clean shutdown: the ``_SHUTDOWN`` sentinel sets a flag that suppresses the
   ``finally`` reschedule, so ``stop_rpc_server`` actually stops the loop.
5. Exception isolation: exceptions inside a task are caught, logged, and
   returned as error strings; they never kill the dispatch loop.
"""

import queue
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

import FreeCADGui
from PySide import QtCore, QtGui, QtWidgets

from rpc_server import dbglog

logger = dbglog.get_logger("gui")

_rpc_request_queue: "queue.Queue[Any]" = queue.Queue()
_SHUTDOWN = object()
_processing = False  # re-entrancy guard: True while process_gui_tasks is draining
_processing_since: float = 0.0  # wall-clock time when _processing became True

# Phantom-input detection for the mouse-button guard. A stuck mouseButtons()
# state (e.g. after a background launch or RDP session) is *static*: same
# buttons and same cursor position on every tick. Real drags always move the
# cursor or change buttons. The guard defers at most this many consecutive
# identical ticks (~500 ms apart), then treats the held button as phantom
# and processes the queue anyway — so a phantom state can never starve RPC,
# even while the FreeCAD window is active.
_PHANTOM_TICK_LIMIT = 20  # 20 x 500 ms = 10 s of motionless button-hold
_last_held_state: "tuple | None" = None
_held_static_ticks = 0

# The static-tick cap alone is NOT enough: a phantom stuck button PLUS a
# live cursor (the user keeps moving the mouse over the active window —
# normal while inspecting a model between run steps) resets the counter on
# every tick, so the queue defers forever (the recurring wedge). Two extra
# bounds make the wedge impossible:
#  1. win32 physical ground truth: GetAsyncKeyState says whether the button
#     is REALLY down. Qt stuck + OS up = phantom, never defer.
#  2. Hold-duration cap: the same nonzero mask held continuously longer
#     than this is phantom even with a moving cursor (a real drag never
#     holds that long; if one ever does, the consequence is merely that a
#     queued task runs during the hold — pre-guard behavior).
_PHANTOM_HOLD_SECONDS = 15.0
_held_mask: "int | None" = None
_held_since: float = 0.0


def _physical_buttons_down() -> "int | None":
    """OS-level physical mouse-button mask on Windows; None elsewhere.

    Bits match Qt LeftButton/RightButton/MiddleButton (1/2/4). Honors the
    left-handed swap setting. Never raises — diagnostics must not break
    dispatch; on any failure returns None (heuristic paths take over).
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        u = ctypes.windll.user32
        if u.GetSystemMetrics(23):  # SM_SWAPBUTTON: left-handed mouse
            vk_l, vk_r = 0x02, 0x01
        else:
            vk_l, vk_r = 0x01, 0x02
        mask = 0
        if u.GetAsyncKeyState(vk_l) & 0x8000:
            mask |= 1
        if u.GetAsyncKeyState(vk_r) & 0x8000:
            mask |= 2
        if u.GetAsyncKeyState(0x04) & 0x8000:  # VK_MBUTTON
            mask |= 4
        return mask
    except Exception:
        return None


def _user_holding_button() -> bool:
    """True while a real user is holding a mouse button in the active window.

    Phantom/stuck states are filtered by three independent checks: physical
    OS state (win32), the static-state tick cap, and the hold-duration cap.
    A real drag inside FreeCAD moves the cursor or changes buttons and the
    OS confirms the hold — only then do RPC tasks wait.
    """
    global _last_held_state, _held_static_ticks, _held_mask, _held_since
    buttons = QtWidgets.QApplication.mouseButtons()
    if buttons == QtCore.Qt.NoButton:
        _last_held_state = None
        _held_static_ticks = 0
        _held_mask = None
        return False
    if not FreeCADGui.getMainWindow().isActiveWindow():
        return False  # button held in some other window; not our drag
    physical = _physical_buttons_down()
    if physical is not None and physical == 0:
        # Qt claims a hold the OS says is not happening: phantom, and the
        # static counter must not keep accumulating for it either.
        logger.debug("mouse guard: phantom hold rejected by OS ground truth (mask=%s)", buttons)
        _last_held_state = None
        _held_static_ticks = 0
        _held_mask = None
        return False
    pos = QtGui.QCursor.pos()
    state = (buttons, pos.x(), pos.y())
    now = time.monotonic()
    if state == _last_held_state:
        _held_static_ticks += 1
    else:
        _last_held_state = state
        _held_static_ticks = 0
    if buttons != _held_mask:
        _held_mask = buttons
        _held_since = now
    if _held_static_ticks >= _PHANTOM_TICK_LIMIT:
        logger.debug("mouse guard: motionless phantom cap hit (%d ticks)", _held_static_ticks)
        return False  # motionless phantom cap
    # continuous-hold cap (phantom + live cursor)
    held_for = now - _held_since
    if held_for >= _PHANTOM_HOLD_SECONDS:
        logger.debug("mouse guard: hold-duration cap hit (%.1fs)", held_for)
        return False
    return True


class _WakeSignal(QtCore.QObject):
    """Qt signal bridge for cross-thread GUI-task wakeup.

    Must be created on the GUI thread (``init_waker``). Emitting from the
    RPC thread is safe: Qt delivers the connection with ``QueuedConnection``,
    so the slot always fires in the GUI thread's event loop.
    """

    _sig = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self._sig.connect(self._on_wake, QtCore.Qt.QueuedConnection)

    def wake(self) -> None:
        self._sig.emit()

    def _on_wake(self) -> None:
        process_gui_tasks(reschedule=False)


_waker: "_WakeSignal | None" = None


def init_waker() -> None:
    """Create the wake-signal bridge. Call once from the GUI thread."""
    global _waker
    _waker = _WakeSignal()


def cleanup_waker() -> None:
    """Release the wake-signal bridge on server stop."""
    global _waker
    _waker = None


def _flush_gui_events(delay_ms: int = 20) -> None:
    FreeCADGui.updateGui()
    app = QtWidgets.QApplication.instance()
    if app is None:
        return

    # ExcludeUserInputEvents: skip mouse/keyboard events to avoid re-entrancy
    # with ongoing navigation. ExcludeSocketNotifiers keeps network I/O out.
    flags = QtCore.QEventLoop.ExcludeUserInputEvents | QtCore.QEventLoop.ExcludeSocketNotifiers
    app.processEvents(flags, delay_ms)
    if delay_ms > 0:
        QtCore.QThread.msleep(delay_ms)
        app.processEvents(flags, delay_ms)


def process_gui_tasks(reschedule: bool = True) -> None:
    """Drain queued GUI-thread callables and optionally reschedule.

    Skips the current tick when any mouse button is held (e.g., 3D navigation
    drag) or when already executing a task (re-entrancy guard). The guard
    prevents ``doc.recompute()`` or ``processEvents()`` inside a task from
    triggering a nested ``process_gui_tasks`` call that corrupts FreeCAD state.

    ``reschedule=False`` is used by the immediate-wake path so it does not
    start a second heartbeat chain alongside the existing 500 ms one.
    """
    global _processing, _processing_since
    if _processing:
        return  # re-entrant call from processEvents inside a task; skip

    shutdown = False
    try:
        if _rpc_request_queue.empty():
            return  # nothing queued; skip cursor/status-bar churn on idle heartbeat ticks
        if _user_holding_button():
            # user is dragging in the active window; defer to next tick.
            # (Phantom/stuck button states are filtered out inside
            # _user_holding_button — they must not starve the queue.)
            logger.debug("mouse guard: deferring queue (real drag in the active window)")
            return
        if QtWidgets.QApplication.activePopupWidget() is not None:
            return  # context menu or popup open; defer to next tick
        if QtWidgets.QApplication.activeModalWidget() is not None:
            return  # modal dialog open; defer to next tick

        _processing = True
        _processing_since = time.monotonic()
        app = QtWidgets.QApplication.instance()
        try:
            status_bar = FreeCADGui.getMainWindow().statusBar()
        except Exception:
            status_bar = None

        if app is not None:
            app.setOverrideCursor(QtCore.Qt.WaitCursor)
        if status_bar is not None:
            status_bar.showMessage("CADPilot: processing…")
        try:
            while not _rpc_request_queue.empty():
                task = _rpc_request_queue.get()
                if task is _SHUTDOWN:
                    shutdown = True
                    logger.info(
                        "GUI dispatch shutting down (queue depth %d)",
                        _rpc_request_queue.qsize(),
                    )
                    return
                try:
                    task()
                except Exception as e:
                    logger.error(
                        "unhandled exception in GUI task: %s: %s",
                        type(e).__name__,
                        e,
                        exc_info=True,
                    )
        finally:
            if app is not None:
                app.restoreOverrideCursor()
            if status_bar is not None:
                status_bar.clearMessage()
    finally:
        _processing = False
        if not shutdown and reschedule:
            QtCore.QTimer.singleShot(500, process_gui_tasks)


def request_shutdown() -> None:
    """Post the sentinel so the next dispatch tick exits without rescheduling."""
    _rpc_request_queue.put(_SHUTDOWN)


def dispatch_to_gui(task: Callable[[], Any], timeout: float = 60) -> Any:
    """Run ``task`` on the GUI thread and return its result.

    Uses a per-call response queue so a timeout in one call never corrupts
    the response for a subsequent call. Wakes the GUI thread immediately via
    a Qt signal instead of waiting for the next 500 ms heartbeat.

    On timeout the queued task is cancelled: if it has not started yet it
    will never run, so a caller retrying after a timeout cannot trigger a
    double execution. A task already running on the GUI thread cannot be
    interrupted; only its result is discarded.

    Returns the task's return value on success, an error string if the task
    raises, or ``{"success": False, "error": ...}`` on timeout.
    """
    response_queue: queue.Queue[Any] = queue.Queue(maxsize=1)
    cancelled = threading.Event()
    # Captured HERE, on the RPC thread: the GUI thread has no request id of its
    # own, so this is what keeps a call's deferred work correlatable with the
    # request that caused it.
    inherited_request = dbglog.request_id()

    def _wrapped() -> None:
        if cancelled.is_set():
            return  # caller timed out and went away; don't run a stale task
        dbglog.set_request_id(inherited_request)
        try:
            res = task()
        except Exception as e:
            logger.error("GUI task raised %s: %s", type(e).__name__, e, exc_info=True)
            res = f"{type(e).__name__}: {e}"
        finally:
            dbglog.clear_request_id()
        response_queue.put(res)

    queued_at = time.monotonic()
    _rpc_request_queue.put(_wrapped)
    if _waker is not None:
        _waker.wake()  # immediate wake via Qt signal (thread-safe)

    try:
        result = response_queue.get(timeout=timeout)
        waited = (time.monotonic() - queued_at) * 1000
        if waited > 1000:
            logger.warning(
                "GUI task waited %.1fms (queue depth %d)", waited, _rpc_request_queue.qsize()
            )
        else:
            # INFO, not DEBUG: this is the line that proves where a call spent
            # its time (RPC thread vs. GUI queue), so it has to be there by
            # default. The mouse-guard deferrals below stay DEBUG — they repeat
            # every 500ms tick during a drag.
            logger.info("GUI task ran after %.1fms", waited)
        return result
    except queue.Empty:
        cancelled.set()  # a not-yet-started task must not run after we give up
        # Diagnose why: if _processing is still True, the GUI thread is occupied
        # by a long-running task that was queued before this one.
        if _processing:
            busy_for = time.monotonic() - _processing_since
            hint = (
                f" (GUI thread has been busy for {busy_for:.1f}s — "
                "consider execute_code_async for heavy OCCT operations)"
            )
            logger.error("GUI dispatch timed out after %ss%s", timeout, hint)
        else:
            hint = ""
            # Idle GUI thread + timeout means the waker/heartbeat chain is dead,
            # not that FreeCAD is busy — the failure mode that used to wedge the
            # addon with nothing in any log to show for it.
            logger.error(
                "GUI dispatch timed out after %ss with an idle GUI thread (queue depth %d) "
                "— the waker/heartbeat chain may be dead",
                timeout,
                _rpc_request_queue.qsize(),
            )
        return {"success": False, "error": f"GUI dispatch timed out after {timeout}s{hint}"}
