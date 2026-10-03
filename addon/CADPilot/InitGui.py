# CADPilot workbench + resident (persistent) toolbar.
#
# Goal: the CADPilot buttons stay visible no matter which workbench is active.
#
# Root cause of the disappearing toolbar — FreeCAD's src/Gui/ToolBarManager.cpp,
# ToolBarManager::setup() runs on EVERY workbench switch:
#   1. toolbars the new workbench does not declare are hidden;
#   2. on the toolbars it keeps, foreign actions are stripped.
# So hosting our buttons on FreeCAD's own "File" toolbar (the previous
# approach) only ever worked behind a re-attach timer.
#
# The approach here: a dedicated "CADPilot" QToolBar owned by the main
# window, with its toggleViewAction() hidden. ToolBarManager::setup() skips
# toolbars whose toggleViewAction is invisible, so it is never hidden and
# never stripped. workbenchActivated + a 2 s watchdog re-assert it anyway,
# covering builds whose hide loop lacks that skip.
#
# Loading constraints this file is written around (verified against
# FreeCAD 1.1: src/Gui/FreeCADGuiInit.py, DirModGui.run_init_gui()):
#   * The file is run with a bare `exec(code)` — NOT imported as a module.
#     Module-level names are therefore not reliably reachable from deferred
#     callbacks, and `__file__` does not point here. Everything below lives
#     inside a single self-contained _bootstrap() that imports what it needs
#     locally and closes over its own variables.
#   * Qt6 moved QAction from QtWidgets to QtGui (`QtWidgets.QAction` raises
#     AttributeError on PySide6); QtGui.QAction is correct for Qt5 and Qt6.
#   * InitGui.py is re-executed when this workbench is activated, so every
#     step is idempotent and one-shot hooks are flagged on the main window.
#
# Diagnostics go through rpc_server.dbglog (ring buffer + rotating file under
# FreeCAD's user data dir + Report View; read them with the get_addon_log tool).
# Should that import fail, this file falls back to <addon dir>/initgui_debug.log
# — bootstrap is the one thing that cannot be debugged any other way.

import contextlib
import os as _os

# Bare exec(): __file__ may be missing or point at the loader, not us.
_ADDON_HINT = None
with contextlib.suppress(NameError):
    _ADDON_HINT = _os.path.dirname(_os.path.abspath(__file__))


def _bootstrap():
    import os
    import sys
    import time
    import traceback

    # ---- locate the addon dir (authoritative: where rpc_server lives) -----
    def find_addon_dir():
        candidates = []
        with contextlib.suppress(NameError):
            # Bare exec(): __file__ may be absent or point at the loader.
            candidates.append(os.path.dirname(os.path.abspath(__file__)))
        try:
            import FreeCAD

            candidates.append(os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "CADPilot"))
        except Exception:
            pass
        candidates += [p for p in sys.path if p]
        for cand in candidates:
            if cand and os.path.exists(os.path.join(cand, "rpc_server", "rpc_server.py")):
                return os.path.normpath(cand)
        return None

    addon_dir = find_addon_dir()
    if addon_dir and addon_dir not in sys.path:
        sys.path.insert(0, addon_dir)
    if not addon_dir:
        try:
            import rpc_server  # already importable → derive it

            addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(rpc_server.__file__)))
        except Exception:
            addon_dir = os.path.dirname(os.path.abspath(sys.argv[0] or "."))
    debug_log = os.path.join(addon_dir, "initgui_debug.log")

    # Logging is the one thing that must survive its own failure: if the
    # framework cannot be imported we still write to a file next to this
    # module, because nothing else can report a broken bootstrap.
    try:
        from rpc_server import dbglog

        dbglog.setup_logging()
        _log = dbglog.get_logger("initgui")
    except Exception:
        _log = None

    def dbg(msg):
        """Self-contained log writer — never touches module-level names."""
        if _log is not None:
            try:
                _log.info(msg)
                return
            except Exception:
                pass
        text = f"=== {time.strftime('%H:%M:%S')} {msg}"
        exc = traceback.format_exc()
        if exc and exc.strip() != "NoneType: None":
            text += "\n" + exc
        try:
            if os.path.exists(debug_log) and os.path.getsize(debug_log) > 512 * 1024:
                os.remove(debug_log)
            with open(debug_log, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except Exception:
            pass

    def dbg_err(msg):
        """A failure: keep the traceback at ERROR level, not buried in INFO."""
        if _log is not None:
            try:
                _log.error(msg, exc_info=True)
                return
            except Exception:
                pass
        dbg(msg)

    dbg(f"bootstrap start addon_dir={addon_dir}")

    try:
        import FreeCAD
        import FreeCADGui
        from PySide import QtCore, QtGui, QtWidgets
    except Exception:
        dbg_err("bootstrap: FreeCAD/PySide import FAILED")
        return

    try:
        from rpc_server import commands as _commands
    except Exception:
        _commands = None
        dbg_err("bootstrap: rpc_server.commands import FAILED")

    # Commands must exist before the workbench menu is built, otherwise
    # FreeCAD warns "Unknown command ..." for every menu entry.
    if _commands is not None:
        try:
            _commands.register_commands()
            dbg("commands registered")
        except Exception:
            dbg_err("register_commands FAILED")

    COMMANDS = [
        "Toggle_Step_Panel",
        "Toggle_RPC_Server",
        "Toggle_Auto_Start",
        "Toggle_Remote_Connections",
        "Configure_Allowed_IPs",
    ]

    # ---- workbench (menu entry in the workbench selector) -----------------
    class CADPilotWorkbench(Workbench):
        MenuText = "CADPilot"
        ToolTip = "CADPilot - let LLMs design in FreeCAD via MCP"

        def Initialize(self):
            # Menu only: the toolbar is the resident global bar, so a
            # workbench-scoped toolbar here would only duplicate it.
            try:
                self.appendMenu("CADPilot", list(COMMANDS))
            except Exception:
                dbg_err("Initialize.appendMenu FAILED")

        def Activated(self):
            pass

        def Deactivated(self):
            pass

        def ContextMenu(self, recipient):
            pass

        def GetClassName(self):
            return "Gui::PythonWorkbench"

    try:
        FreeCADGui.addWorkbench(CADPilotWorkbench())
        dbg("workbench registered")
    except Exception:
        dbg_err("addWorkbench FAILED")

    # ---- resident toolbar -------------------------------------------------
    def build_toolbar_actions(mw):
        """Self-owned QActions. FreeCAD's command actions materialize lazily
        and carry non-command objectNames; own actions keep the bar
        self-contained and the check states ours to sync."""
        actions = []
        for object_name, label, checkable, handler in _commands.toolbar_button_specs():
            action = QtGui.QAction(label, mw)  # QtGui, NOT QtWidgets (Qt6)
            action.setObjectName(object_name)
            action.setCheckable(checkable)
            # Tag ours: FreeCAD's own command actions share the same objectNames,
            # and the leak-prune below must never delete those.
            action.setProperty("CADPilotOwned", True)
            # Connect through a closure that BINDS the handler as a default
            # argument. PySide6 holds only a weak reference to a bound method of
            # a plain Python object, so `connect(handler.Activated)` goes dead the
            # moment the `handler` local is collected (the loop's temporary list
            # is dropped right after): the button then toggles its check mark and
            # calls nothing. The closure keeps the handler — and the slot — alive.
            if checkable:
                action.toggled.connect(lambda checked=False, h=handler: h.Activated(checked))
            else:
                action.triggered.connect(lambda _=False, h=handler: h.Activated())
            actions.append(action)
        return actions

    def setup_toolbar():
        if _commands is None:
            return
        bar_name = "CADPilot"
        try:
            mw = FreeCADGui.getMainWindow()
        except Exception:
            dbg("setup_toolbar: no main window")
            return

        def ensure_bar(reason):
            try:
                bar = mw.findChild(QtWidgets.QToolBar, bar_name)
                created = False
                if bar is None:
                    bar = QtWidgets.QToolBar(bar_name, mw)
                    bar.setObjectName(bar_name)
                    bar.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
                    mw.addToolBar(QtCore.Qt.TopToolBarArea, bar)
                    created = True
                needed = {spec[0] for spec in _commands.toolbar_button_specs()}
                have = {a.objectName() for a in bar.actions()}
                if not needed <= have:  # fresh, or stripped by a switch
                    for action in list(bar.actions()):
                        if action.objectName() in needed:
                            bar.removeAction(action)
                            action.deleteLater()
                    for action in build_toolbar_actions(mw):
                        bar.addAction(action)
                # Prune ours that were detached from the bar in an earlier build
                # but are still parented to the main window: they linger in
                # findChildren-based check-state syncs and confuse the toggle.
                attached = bar.actions()
                for action in mw.findChildren(QtGui.QAction):
                    if action.property("CADPilotOwned") and action not in attached:
                        action.deleteLater()
                # Immunity: ToolBarManager skips toolbars whose toggleViewAction
                # is invisible. Does NOT hide the bar itself.
                bar.toggleViewAction().setVisible(False)
                if not bar.isVisible():
                    bar.show()
                dbg(f"ensure_bar({reason}) created={created} actions={len(bar.actions())}")
            except Exception:
                dbg(f"ensure_bar({reason}) FAILED")

        def on_workbench_activated(*_args):
            # Fires mid-switch; ToolBarManager::setup() may run around it.
            QtCore.QTimer.singleShot(0, lambda: ensure_bar("wb+0ms"))
            QtCore.QTimer.singleShot(300, lambda: ensure_bar("wb+300ms"))

        if not mw.property("CADPilot_GlobalHooked"):
            mw.setProperty("CADPilot_GlobalHooked", True)
            try:
                mw.workbenchActivated.connect(on_workbench_activated)
                dbg("workbenchActivated hooked")
            except Exception:
                dbg_err("workbenchActivated hook FAILED")

        if not mw.property("CADPilot_Watchdog"):
            mw.setProperty("CADPilot_Watchdog", True)

            def watch():
                try:
                    bar = mw.findChild(QtWidgets.QToolBar, bar_name)
                    needed = {spec[0] for spec in _commands.toolbar_button_specs()}
                    have = {a.objectName() for a in bar.actions()} if bar else set()
                    if bar is None or not needed <= have or not bar.isVisible():
                        ensure_bar("watchdog")
                except Exception:
                    pass
                QtCore.QTimer.singleShot(2000, watch)

            QtCore.QTimer.singleShot(2000, watch)
            dbg("watchdog started")

        ensure_bar("startup")
        try:
            _commands.schedule_toggle_sync()
        except Exception:
            dbg_err("schedule_toggle_sync FAILED")

    def setup_panel():
        try:
            from rpc_server import step_panel

            step_panel.ensure_panel()
            dbg("step panel ready")
        except Exception:
            dbg_err("setup_panel FAILED")

    def autostart():
        try:
            from rpc_server import rpc_server

            settings = rpc_server.load_settings()
            dbg(f"autostart setting={settings.get('auto_start_rpc')}")
            if settings.get("auto_start_rpc", False):
                msg = rpc_server.start_rpc_server()
                FreeCAD.Console.PrintMessage(f"[CADPilot] Auto-start: {msg}\n")
        except Exception:
            dbg_err("autostart FAILED")
        if _commands is not None:
            try:
                _commands.sync_all_toggle_states()
            except Exception:
                dbg_err("sync_all_toggle_states FAILED")

    try:
        QtCore.QTimer.singleShot(1000, setup_toolbar)
        QtCore.QTimer.singleShot(1200, autostart)
        QtCore.QTimer.singleShot(1400, setup_panel)
        dbg("deferred setup scheduled")
    except Exception:
        dbg_err("schedule deferred setup FAILED")


try:
    _bootstrap()
except BaseException:
    # Last-resort diagnostics: builtins only, no module-level names.
    try:
        import os as _o
        import time as _t
        import traceback as _tb

        _p = _ADDON_HINT or "."
        with open(_o.path.join(_p, "initgui_debug.log"), "a", encoding="utf-8") as _f:
            _f.write(f"=== {_t.strftime('%H:%M:%S')} bootstrap crashed\n{_tb.format_exc()}\n")
    except BaseException:
        pass
