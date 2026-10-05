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
   mouse buttons are held so MCP tasks cannot interrupt 3D navigation drags,
   and re-checks the guard BETWEEN queued tasks so a press that begins
   mid-drain pauses the queue instead of starving user input for the whole
   backlog. On Windows the OS physical button state (GetAsyncKeyState) is
   authoritative in both directions — Qt's mouseButtons() is only refreshed
   when the event loop delivers button events, which is stale exactly while
   the queue is busy, so consulting it alone misses fresh presses and late
   releases (the race that let tasks punch through mid-drag). Off Windows,
   phantom/stuck states are filtered by a static-state time cap and a
   hold-duration cap — see ``_user_holding_button``.
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

# Phantom-input detection for the mouse-button guard — only needed where the
# OS cannot report the physical button state (i.e. off Windows; on win32 the
# ground truth makes phantoms impossible to mistake, see _user_holding_button).
# A stuck mouseButtons() state (e.g. after a background launch or RDP session)
# is *static*: same buttons and same cursor position for a long stretch. Real
# drags always move the cursor or change buttons. The guard treats a state
# that has been identical for this many seconds as phantom and processes the
# queue anyway. Measured in TIME, not ticks: the guard is also evaluated
# between queued tasks now, so evaluations are no longer ~500 ms apart.
_PHANTOM_STATIC_SECONDS = 10.0
_last_held_state: "tuple | None" = None
_held_static_since: "float | None" = None

# The static cap alone is NOT enough: a phantom stuck button PLUS a live
# cursor (the user keeps moving the mouse over the active window — normal
# while inspecting a model between run steps) resets the static timer on
# every evaluation, so the queue would defer forever (the recurring wedge).
# The hold-duration cap bounds that: the same nonzero mask held continuously
# longer than this is treated as phantom even with a moving cursor. Both caps
# are heuristic-path only — on win32 the OS says whether the button is REALLY
# down, and a real hold must never be capped: punching a task through a long
# inspection drag at the 15 s mark is precisely the freeze users feel.
_PHANTOM_HOLD_SECONDS = 15.0
_held_mask: "int | None" = None
_held_since: float = 0.0

# Why the queue was last deferred, and for how long. Written on the GUI thread by
# process_gui_tasks, read on the RPC thread by the timeout path — a plain str/float
# is atomic enough in CPython and this is only a diagnosis.
#
# Without it the timeout message blamed the waker unconditionally, which is wrong
# exactly when a human is interacting: the heartbeat is healthy, the queue simply
# must not be drained during a drag, an open context menu or a modal dialog. The
# reported symptom ("it happens when I operate FreeCAD at the same time as the
# model; the process CPU barely moves") is that case, not a dead chain.
_DEFER_LABELS = {
    "button": "the user is holding a mouse button in the FreeCAD window",
    "popup": "a popup menu is open",
    "modal": "a modal dialog is open",
}
_DEFER_WARN_SECONDS = 10.0
_defer_reason: "str | None" = None
_defer_since: float = 0.0
_defer_warned = False

# How long a dispatch waits before it reports user-interaction back-pressure
# instead of waiting out its whole timeout. The guard will not drain the queue
# while the interaction lasts, so a 60s wait only turns an already-known outcome
# into a late one: the caller sat in silence for a minute to be told "release
# the mouse button". Long enough that an ordinary click (a few hundred ms)
# still completes normally.
_USER_HOLD_GRACE = 2.0
_MISSING = object()


def _note_defer(reason: str) -> None:
    """Record a deferral, warning once when it outlasts _DEFER_WARN_SECONDS.

    The warning is what makes a stalled queue visible in get_addon_log while it
    is happening — otherwise the only trace is a client-side timeout.
    """
    global _defer_reason, _defer_since, _defer_warned
    now = time.monotonic()
    if _defer_reason != reason:
        _defer_reason = reason
        _defer_since = now
        _defer_warned = False
        return
    if not _defer_warned and now - _defer_since >= _DEFER_WARN_SECONDS:
        _defer_warned = True
        logger.warning(
            "GUI queue deferred for %.1fs by %s (queue depth %d) — RPC calls will time out "
            "until it clears; the heartbeat is fine, this is back-pressure",
            now - _defer_since,
            _DEFER_LABELS.get(reason, reason),
            _rpc_request_queue.qsize(),
        )


def _clear_defer() -> None:
    global _defer_reason, _defer_warned
    _defer_reason = None
    _defer_warned = False


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


def _reset_hold_trackers() -> None:
    """Forget all held-button tracking state (heuristic path bookkeeping)."""
    global _last_held_state, _held_static_since, _held_mask
    _last_held_state = None
    _held_static_since = None
    _held_mask = None


def _user_holding_button() -> bool:
    """True while a real user is holding a mouse button in the active window.

    On Windows the OS physical state is authoritative in BOTH directions.
    Qt's mouseButtons() is only updated when the event loop delivers button
    events, so it is stale exactly while the queue is busy: a fresh press is
    invisible until the running task finishes (tasks used to start mid-drag)
    and a release is invisible too (defers outlasted the drag). Consulting
    GetAsyncKeyState only to REJECT phantom holds — the old design — fixed
    the starvation direction but left the interrupt-the-user direction racy.
    A nonzero physical mask with the FreeCAD window active is a REAL hold:
    never capped, because capping it is what punched a task through a long
    inspection drag at the 15 s mark.

    Off Windows there is no ground truth, so the Qt state decides, with two
    phantom filters (static-state time cap, hold-duration cap) so a stuck
    button state can never starve the queue.
    """
    global _last_held_state, _held_static_since, _held_mask, _held_since
    physical = _physical_buttons_down()
    if physical is not None:
        if physical == 0 or not FreeCADGui.getMainWindow().isActiveWindow():
            # OS says no button is down (any Qt-held state is a stale phantom),
            # or the hold belongs to some other window. The inactive branch
            # must reset too: a stale _held_since would otherwise trip the
            # hold cap the moment the user comes back mid-hold.
            _reset_hold_trackers()
            return False
        return True
    buttons = QtWidgets.QApplication.mouseButtons()
    if buttons == QtCore.Qt.NoButton:
        _reset_hold_trackers()
        return False
    if not FreeCADGui.getMainWindow().isActiveWindow():
        _reset_hold_trackers()  # same stale-_held_since trap as above
        return False
    pos = QtGui.QCursor.pos()
    state = (buttons, pos.x(), pos.y())
    now = time.monotonic()
    if state != _last_held_state:
        _last_held_state = state
        _held_static_since = now
    if buttons != _held_mask:
        _held_mask = buttons
        _held_since = now
    if _held_static_since is not None and now - _held_static_since >= _PHANTOM_STATIC_SECONDS:
        logger.debug("mouse guard: motionless phantom cap hit (%.1fs)", now - _held_static_since)
        return False  # motionless phantom cap
    held_for = now - _held_since
    if held_for >= _PHANTOM_HOLD_SECONDS:
        logger.debug("mouse guard: hold-duration cap hit (%.1fs)", held_for)
        return False  # continuous-hold cap (phantom + live cursor)
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
            _clear_defer()
            return  # nothing queued; skip cursor/status-bar churn on idle heartbeat ticks
        if _user_holding_button():
            # user is dragging in the active window; defer to next tick.
            # (Phantom/stuck button states are filtered out inside
            # _user_holding_button — they must not starve the queue.)
            _note_defer("button")
            logger.debug("mouse guard: deferring queue (real drag in the active window)")
            return
        if QtWidgets.QApplication.activePopupWidget() is not None:
            _note_defer("popup")
            return  # context menu or popup open; defer to next tick
        if QtWidgets.QApplication.activeModalWidget() is not None:
            _note_defer("modal")
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
            ran_one = False
            while not _rpc_request_queue.empty():
                if ran_one:
                    # Re-check the interaction guards BETWEEN tasks: the checks
                    # above only run when a drain starts, so without this a
                    # press that begins mid-drain (or a dialog a task opened)
                    # cannot pause the queue and user input starves for the
                    # whole backlog. Pausing returns to the event loop, which
                    # delivers the pending input; the 500 ms heartbeat (and the
                    # next dispatch's wake) resumes the drain once it clears.
                    if _user_holding_button():
                        _note_defer("button")
                        logger.debug("mouse guard: user input mid-drain; pausing queue")
                        return
                    if QtWidgets.QApplication.activePopupWidget() is not None:
                        _note_defer("popup")
                        return
                    if QtWidgets.QApplication.activeModalWidget() is not None:
                        _note_defer("modal")
                        return
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
                ran_one = True
            _clear_defer()  # drained: any earlier deferral is over
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

    deadline = queued_at + timeout
    result: Any = _MISSING
    deferred_reason: str | None = None
    while result is _MISSING:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            result = response_queue.get(timeout=min(remaining, _USER_HOLD_GRACE))
        except queue.Empty:
            if _defer_reason is not None:
                # The guards are holding the queue back on purpose and will keep
                # doing so while the interaction lasts, so waiting out the rest
                # of the timeout cannot succeed — report the actionable reason
                # now. A short click still lands inside _USER_HOLD_GRACE above.
                deferred_reason = _defer_reason
                break
            # Otherwise keep waiting: a busy GUI thread finishes on its own.
    if result is not _MISSING:
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

    # Timed out (or gave up early on user interaction): diagnose why.
    cancelled.set()  # a not-yet-started task must not run after we give up
    if deferred_reason is not None:
        # The guards are holding the queue back on purpose: the user is
        # mid-interaction. Nothing is broken and nothing needs restarting.
        label = _DEFER_LABELS.get(deferred_reason, deferred_reason)
        deferred_for = time.monotonic() - _defer_since
        logger.error(
            "GUI dispatch reported after %.1fs — %s (queue depth %d, deferred %.1fs); "
            "back-pressure from user interaction, not a dead dispatcher",
            _USER_HOLD_GRACE,
            label,
            _rpc_request_queue.qsize(),
            deferred_for,
        )
        return {
            "success": False,
            "error": (
                f"CADPilot could not act on the document within {_USER_HOLD_GRACE:.0f}s because "
                f"{label}. The queued work is held back deliberately while the user interacts, "
                "so nothing is stuck: close the dialog or menu (or release the mouse button) "
                "and retry; the queue drains by itself."
            ),
        }
    if _processing:
        busy_for = time.monotonic() - _processing_since
        hint = (
            f" (GUI thread has been busy for {busy_for:.1f}s — "
            "consider execute_code_async for heavy OCCT operations)"
        )
        logger.error("GUI dispatch timed out after %ss%s", timeout, hint)
        return {"success": False, "error": f"GUI dispatch timed out after {timeout}s{hint}"}
    if _defer_reason is not None:
        # Defensive: the deferral started in the last moments of the wait.
        label = _DEFER_LABELS.get(_defer_reason, _defer_reason)
        logger.error(
            "GUI dispatch timed out after %ss — %s (queue depth %d)",
            timeout,
            label,
            _rpc_request_queue.qsize(),
        )
        return {
            "success": False,
            "error": (
                f"GUI dispatch timed out after {timeout}s because {label} — CADPilot must "
                "not act on the document mid-interaction, so the queued work is held back. "
                "Nothing is stuck: close the dialog or menu (or release the mouse button) "
                "and retry; the queue drains by itself."
            ),
        }
    # Idle GUI thread + timeout means the waker/heartbeat chain is dead,
    # not that FreeCAD is busy — the failure mode that used to wedge the
    # addon with nothing in any log to show for it.
    logger.error(
        "GUI dispatch timed out after %ss with an idle GUI thread (queue depth %d) "
        "— the waker/heartbeat chain may be dead",
        timeout,
        _rpc_request_queue.qsize(),
    )
    return {
        "success": False,
        "error": (
            f"GUI dispatch timed out after {timeout}s: nothing is holding the queue and "
            "the GUI thread is idle, so the waker/heartbeat chain may be dead. Repair it "
            "with execute_code_async (its worker needs no GUI dispatch); see the addon "
            "section of AGENTS.md."
        ),
    }
