"""Qt Command classes for the CADPilot workbench menu.

Defines the four toolbar/menu entries (checkable RPC Server start/stop
toggle, Toggle Auto-Start, Toggle Remote, Configure Allowed IPs), plus the
post-startup sync that reflects saved settings on the checkable items.

``register_commands()`` and ``schedule_toggle_sync()`` are invoked from
``rpc_server.py`` at import time to preserve current side-effect behavior.
"""

import FreeCAD
import FreeCADGui

# QAction lives in QtGui (Qt5 and Qt6 alike) — QtWidgets.QAction only exists
# in Qt4 and raises AttributeError on FreeCAD 1.1 (PySide6).
from PySide import QtCore, QtGui, QtWidgets

from rpc_server.ip_filter import validate_allowed_ips
from rpc_server.settings import load_settings, save_settings


def _set_actions_checked(object_name: str, value: bool) -> None:
    """Set the check state of every QAction with this objectName.

    The same command appears as a workbench-scoped action and as a persistent
    global action (see InitGui) — keep every instance in sync. Signals are
    blocked: this reflects state, it must not re-enter the command's Activated
    (which would re-run the toggle and flip-flop between instances).
    """
    try:
        main_window = FreeCADGui.getMainWindow()
        for action in main_window.findChildren(QtGui.QAction):
            if action.objectName() == object_name:
                blocked = action.blockSignals(True)
                action.setChecked(value)
                action.blockSignals(blocked)
    except Exception:
        pass


class ToggleRPCServerCommand:
    """Checkable Start/Stop switch: checked = server running."""

    def GetResources(self):
        return {
            "MenuText": "RPC Server",
            "ToolTip": "RPC Server — checked: running, unchecked: stopped",
            "Checkable": True,
        }

    def Activated(self, checked=0):
        from . import rpc_server  # late import: avoids circular at module load

        if checked:
            msg = rpc_server.start_rpc_server()
            FreeCAD.Console.PrintMessage(msg + "\n")
            if "already running" in msg:
                # State drifted from the UI (e.g. auto-start ran first);
                # snap the checkbox back to reality instead of lying.
                QtCore.QTimer.singleShot(0, lambda: _set_actions_checked("Toggle_RPC_Server", True))
            elif "still stopping" in msg:
                # Start refused: the old socket hasn't drained yet. Uncheck so
                # the UI invites a retry instead of showing a false "running".
                QtCore.QTimer.singleShot(
                    0, lambda: _set_actions_checked("Toggle_RPC_Server", False)
                )
            return

        msg = rpc_server.stop_rpc_server()
        FreeCAD.Console.PrintMessage(msg + "\n")
        if "was not running" in msg:
            QtCore.QTimer.singleShot(0, lambda: _set_actions_checked("Toggle_RPC_Server", False))

    def _set_checked(self, value: bool) -> None:
        _set_actions_checked("Toggle_RPC_Server", value)

    def IsActive(self):
        return True


class ToggleRemoteConnectionsCommand:
    def GetResources(self):
        return {
            "MenuText": "Remote Connections",
            "ToolTip": "Enable or disable remote connections for the RPC server.",
            "Checkable": True,
        }

    def Activated(self, checked=0):
        from . import rpc_server

        settings = load_settings()
        settings["remote_enabled"] = bool(checked)
        save_settings(settings)

        if settings["remote_enabled"]:
            allowed_ips = settings.get("allowed_ips", "127.0.0.1")
            FreeCAD.Console.PrintMessage(
                f"Remote connections enabled. Allowed IPs: {allowed_ips}\n"
            )
        else:
            FreeCAD.Console.PrintMessage("Remote connections disabled.\n")

        if rpc_server.rpc_server_instance:
            FreeCAD.Console.PrintMessage("Restart the RPC server for changes to take effect.\n")

    def IsActive(self):
        return True


class ConfigureAllowedIPsCommand:
    def GetResources(self):
        return {
            "MenuText": "Configure Allowed IPs",
            "ToolTip": "Set which IP addresses or subnets are allowed to connect to the RPC server.",
        }

    def Activated(self):
        from . import rpc_server

        settings = load_settings()
        current_ips = settings.get("allowed_ips", "127.0.0.1")
        text, ok = QtWidgets.QInputDialog.getText(
            None,
            "Allowed IP Addresses",
            "Enter allowed IP addresses or subnets (comma-separated):\n"
            "Examples: 127.0.0.1, 192.168.1.0/24, 10.0.0.5",
            QtWidgets.QLineEdit.Normal,
            current_ips,
        )
        if ok and text.strip():
            valid, errors = validate_allowed_ips(text.strip())
            if errors:
                QtWidgets.QMessageBox.warning(
                    None,
                    "Invalid IP Configuration",
                    "The following errors were found:\n\n"
                    + "\n".join(f"• {e}" for e in errors)
                    + (
                        "\n\nOnly valid entries will be saved."
                        if valid
                        else "\n\nNo valid entries found. Settings not changed."
                    ),
                )
            if not valid:
                FreeCAD.Console.PrintWarning("Allowed IPs not changed — no valid entries.\n")
                return
            normalised = ", ".join(valid)
            settings["allowed_ips"] = normalised
            save_settings(settings)
            FreeCAD.Console.PrintMessage(f"Allowed IPs updated to: {normalised}\n")
            if rpc_server.rpc_server_instance:
                FreeCAD.Console.PrintMessage("Restart the RPC server for changes to take effect.\n")
        else:
            FreeCAD.Console.PrintMessage("Allowed IPs not changed.\n")

    def IsActive(self):
        return True


class ToggleAutoStartCommand:
    def GetResources(self):
        return {
            "MenuText": "Auto-Start Server",
            "ToolTip": "Automatically start the RPC server when FreeCAD launches.",
            "Checkable": True,
        }

    def Activated(self, checked=0):
        settings = load_settings()
        settings["auto_start_rpc"] = bool(checked)
        save_settings(settings)

        if settings["auto_start_rpc"]:
            FreeCAD.Console.PrintMessage(
                "CADPilot server will start automatically on next FreeCAD launch.\n"
            )
        else:
            FreeCAD.Console.PrintMessage("CADPilot server auto-start disabled.\n")

    def IsActive(self):
        return True


class ToggleStepPanelCommand:
    """Show/hide the CADPilot steps panel (step management & replay)."""

    def GetResources(self):
        return {
            "MenuText": "Steps",
            "ToolTip": "Step management & replay — inspect, single-step, roll back, "
            "edit parameters and re-run",
            "Checkable": True,
        }

    def Activated(self, checked=0):
        from rpc_server import step_panel

        step_panel.show_step_panel(bool(checked))
        # Several instances of this action can exist (workbench menu + the
        # resident global bar); point them all at what the dock actually did.
        QtCore.QTimer.singleShot(
            0, lambda: _set_actions_checked("Toggle_Step_Panel", step_panel.panel_visible())
        )

    def IsActive(self):
        return True


def register_commands() -> None:
    if register_commands._done:  # FreeCAD re-imports InitGui on every workbench switch
        return
    FreeCADGui.addCommand("Toggle_Step_Panel", ToggleStepPanelCommand())
    FreeCADGui.addCommand("Toggle_RPC_Server", ToggleRPCServerCommand())
    FreeCADGui.addCommand("Toggle_Auto_Start", ToggleAutoStartCommand())
    FreeCADGui.addCommand("Toggle_Remote_Connections", ToggleRemoteConnectionsCommand())
    FreeCADGui.addCommand("Configure_Allowed_IPs", ConfigureAllowedIPsCommand())
    register_commands._done = True


register_commands._done = False


def toolbar_button_specs():
    """(objectName, label, checkable, handler) for the persistent global toolbar.

    InitGui builds plain Qt actions from these specs instead of reusing
    FreeCAD's command QActions — those materialize lazily (only once their
    workbench toolbar is built) and carry non-command objectNames, so
    findChild-by-command-id kept returning None and left our bar empty.
    Own actions make the bar self-contained and the check states ours to sync.
    """
    return [
        ("Toggle_Step_Panel", "Steps", True, ToggleStepPanelCommand()),
        ("Toggle_RPC_Server", "RPC Server", True, ToggleRPCServerCommand()),
        ("Toggle_Auto_Start", "Auto-Start Server", True, ToggleAutoStartCommand()),
        ("Toggle_Remote_Connections", "Remote Connections", True, ToggleRemoteConnectionsCommand()),
        ("Configure_Allowed_IPs", "Configure Allowed IPs", False, ConfigureAllowedIPsCommand()),
    ]


def sync_all_toggle_states() -> None:
    """Set every persistent-bar toggle to its truthful state.

    Runs from InitGui right after the bar is built (and after auto-start), so
    the check marks match reality instead of Qt's unchecked default.
    """
    from . import rpc_server

    settings = load_settings()
    _set_actions_checked("Toggle_RPC_Server", bool(rpc_server.rpc_server_instance))
    _set_actions_checked("Toggle_Auto_Start", bool(settings.get("auto_start_rpc", False)))
    _set_actions_checked("Toggle_Remote_Connections", bool(settings.get("remote_enabled", False)))
    _set_actions_checked("Toggle_Step_Panel", _panel_visible())


def _panel_visible() -> bool:
    try:
        from rpc_server import step_panel

        return step_panel.panel_visible()
    except Exception:
        return False


# Map command objectName -> settings key. Matching on objectName rather than
# the localized menu text keeps this working under translation.
_TOGGLE_COMMANDS = {
    "Toggle_Remote_Connections": "remote_enabled",
    "Toggle_Auto_Start": "auto_start_rpc",
}

# Checkable entries of the persistent bar. The startup sync uses this only to
# decide when it has found every action it is waiting for.
_TOGGLE_ACTIONS = (
    "Toggle_RPC_Server",
    "Toggle_Remote_Connections",
    "Toggle_Auto_Start",
    "Toggle_Step_Panel",
)

_SYNC_MAX_RETRIES = 10  # ~20 s at 2 s/retry before giving up


def _sync_toggle_states(retries_left: int = _SYNC_MAX_RETRIES) -> None:
    """Sync checkable actions with saved settings / runtime state on startup.

    The menu actions are created asynchronously, so retry a bounded number of
    times until they exist rather than polling forever. The RPC-server toggle
    reflects runtime state (rpc_server_instance) and the steps toggle reflects
    the dock's actual visibility, instead of saved settings.
    """
    try:
        from . import rpc_server

        settings = load_settings()
        main_window = FreeCADGui.getMainWindow()
        found = 0
        for action in main_window.findChildren(QtGui.QAction):
            name = action.objectName()
            if name == "Toggle_RPC_Server":
                action.setChecked(bool(rpc_server.rpc_server_instance))
                found += 1
                continue
            if name == "Toggle_Step_Panel":
                action.setChecked(_panel_visible())
                found += 1
                continue
            key = _TOGGLE_COMMANDS.get(name)
            if key is not None:
                action.setChecked(bool(settings.get(key, False)))
                found += 1
        if found >= len(_TOGGLE_ACTIONS):
            return
    except Exception:
        pass
    if retries_left > 0:
        QtCore.QTimer.singleShot(2000, lambda: _sync_toggle_states(retries_left - 1))


def schedule_toggle_sync() -> None:
    QtCore.QTimer.singleShot(2000, _sync_toggle_states)
