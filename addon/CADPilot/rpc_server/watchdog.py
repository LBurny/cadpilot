"""RPC server watchdog: revive the endpoint when it is down but wanted.

The watchdog owns the "should the RPC server be running right now" intent and
lives OUTSIDE the modules a hot reload touches, so a failed or crashed
restart cannot leave the endpoint dead:

  * A QTimer parented to FreeCAD's main window runs ``tick()`` every 3 s. The
    timer survives module reloads (Qt owns it, the callback is a plain module
    function) and the tick resolves ``rpc_server.rpc_server`` FRESH each
    time, so it always calls the currently-loaded code.
  * Intent is a main-window property (``CADPilot_RPC_Desired``), not a module
    global, for the same reason. Every start attempt sets it True; the
    toolbar toggles set it False (a user decision must stick); the first
    tick after boot initializes it from ``auto_start_rpc``.
  * ``CADPilot_RPC_SuppressUntil`` lets ``restart_rpc_server`` own the revive
    window while it stops, reloads and rebinds, so the watchdog cannot start
    a half-reloaded module graph behind its back.

Consequence worth documenting: a PROGRAMMATIC stop (an execute_code snippet)
leaves the intent untouched, so the watchdog revives the endpoint within one
tick. A stop that should stick goes through the toolbar toggle, which records
the intent before stopping.
"""

import importlib
import time

import FreeCADGui
from PySide import QtCore

from rpc_server import dbglog

PROPERTY = "CADPilot_RPC_Watchdog"
DESIRED_PROPERTY = "CADPilot_RPC_Desired"
SUPPRESS_PROPERTY = "CADPilot_RPC_SuppressUntil"

TICK_MS = 3000


def _log():
    return dbglog.get_logger("watchdog")


def _main_window():
    try:
        return FreeCADGui.getMainWindow()
    except Exception:
        return None


def set_desired(value: bool) -> None:
    """Record whether the endpoint should be running (survives hot reloads)."""
    mw = _main_window()
    if mw is not None:
        mw.setProperty(DESIRED_PROPERTY, bool(value))


def desired() -> bool:
    """The recorded intent; the first read initializes it from settings."""
    mw = _main_window()
    if mw is None:
        return False
    value = mw.property(DESIRED_PROPERTY)
    if value is None:
        from rpc_server.settings import load_settings

        value = bool(load_settings().get("auto_start_rpc", False))
        mw.setProperty(DESIRED_PROPERTY, value)
    return bool(value)


def suppress(seconds: float) -> None:
    """Defer the watchdog for ``seconds`` (0 clears it)."""
    mw = _main_window()
    if mw is not None:
        mw.setProperty(SUPPRESS_PROPERTY, time.time() + max(0.0, float(seconds)))


def suppress_until() -> float:
    mw = _main_window()
    if mw is None:
        return 0.0
    try:
        return float(mw.property(SUPPRESS_PROPERTY) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def server_is_down(rs) -> bool:
    """Down = no instance, or serve_forever died under a live instance."""
    if rs.rpc_server_instance is None:
        return True
    thread = rs.rpc_server_thread
    return thread is not None and not thread.is_alive()


_fail_streak = 0


def tick() -> dict:
    """One watchdog pass; returns what it saw (handy for debugging)."""
    global _fail_streak

    rs = importlib.import_module("rpc_server.rpc_server")
    info: dict = {
        "desired": desired(),
        "suppressed": time.time() < suppress_until(),
        "down": None,
        "action": "idle",
    }
    if info["suppressed"]:
        return info
    if not info["desired"]:
        _fail_streak = 0
        return info
    if not server_is_down(rs):
        info["down"] = False
        _fail_streak = 0
        return info
    info["down"] = True

    if rs.rpc_server_instance is not None:
        # serve_forever died under a live instance. stop_rpc_server clears the
        # husk (its shutdown/join/server_close run on the bounded drain
        # thread); the start itself happens on the next tick.
        info["action"] = "stop-husk"
        _log().warning("watchdog: RPC server thread died; clearing it, restart next tick")
        try:
            rs.stop_rpc_server()
        except Exception:
            _log().error("watchdog: clearing the dead server failed", exc_info=True)
        return info

    try:
        msg = str(rs.start_rpc_server())
    except Exception as e:
        msg = f"start raised: {e}"
    info["action"] = "start-attempt"
    if "started at" in msg or "already running" in msg:
        _fail_streak = 0
        _log().info("watchdog: RPC server revived (%s)", msg)
    else:
        _fail_streak += 1
        if _fail_streak == 1 or _fail_streak % 5 == 0:
            _log().warning(
                "watchdog: start attempt %d failed (%s); retrying next tick",
                _fail_streak,
                msg,
            )
    return info


def ensure_started() -> str:
    """Install the tick timer (idempotent; call from InitGui or after reload)."""
    mw = _main_window()
    if mw is None:
        return "watchdog unavailable (no main window)"
    if mw.property(PROPERTY) is not None:
        return "watchdog already running"
    timer = QtCore.QTimer(mw)  # Qt owns it: outlives this call and any reload
    timer.setInterval(TICK_MS)
    timer.timeout.connect(_on_timer)
    timer.start()
    mw.setProperty(PROPERTY, True)
    _log().info("watchdog: started (tick %d ms)", TICK_MS)
    return "watchdog started"


def _on_timer():
    try:
        tick()
    except Exception:
        _log().error("watchdog: tick failed", exc_info=True)
