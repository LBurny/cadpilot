import os as _os
import sys as _sys

try:
    _addon_dir = _os.path.dirname(_os.path.abspath(__file__))
except NameError:
    import inspect as _inspect

    _addon_dir = _os.path.dirname(_os.path.abspath(_inspect.getfile(_inspect.currentframe())))
if _addon_dir not in _sys.path:
    _sys.path.insert(0, _addon_dir)


class CADPilotWorkbench(Workbench):
    MenuText = "CADPilot"
    ToolTip = "CADPilot - let LLMs design in FreeCAD via MCP"

    def Initialize(self):

        commands = [
            "Start_RPC_Server",
            "Stop_RPC_Server",
            "Toggle_Auto_Start",
            "Toggle_Remote_Connections",
            "Configure_Allowed_IPs",
        ]
        self.appendToolbar("CADPilot", commands)
        self.appendMenu("CADPilot", commands)

    def Activated(self):
        pass

    def Deactivated(self):
        pass

    def ContextMenu(self, recipient):
        pass

    def GetClassName(self):
        return "Gui::PythonWorkbench"


Gui.addWorkbench(CADPilotWorkbench())


def _auto_start_mcp():
    try:
        from rpc_server import rpc_server

        settings = rpc_server.load_settings()
        if not settings.get("auto_start_rpc", False):
            return

        msg = rpc_server.start_rpc_server()
        FreeCAD.Console.PrintMessage(f"[CADPilot] Auto-start: {msg}\n")
    except Exception as e:
        FreeCAD.Console.PrintWarning(f"[CADPilot] Auto-start failed: {e}\n")


from PySide import QtCore

QtCore.QTimer.singleShot(0, _auto_start_mcp)


def _reorder_workbench_list():
    """Move CADPilot right after BIM in the workbench selector list."""
    try:
        import FreeCADGui as Gui
        from PySide import QtWidgets

        mw = Gui.getMainWindow()
        for widget in mw.findChildren(QtWidgets.QComboBox):
            if widget.count() < 10:
                continue
            cadpilot_idx = None
            bim_idx = None
            for i in range(widget.count()):
                text = widget.itemText(i)
                if text == "CADPilot":
                    cadpilot_idx = i
                elif text == "BIM":
                    bim_idx = i
            if cadpilot_idx is not None and bim_idx is not None:
                target_idx = bim_idx + 1
                if cadpilot_idx == target_idx:
                    return  # Already in the right place
                item_data = widget.itemData(cadpilot_idx)
                item_icon = widget.itemIcon(cadpilot_idx)
                widget.removeItem(cadpilot_idx)
                # Adjust target if removing shifted indices
                if cadpilot_idx < target_idx:
                    target_idx -= 1
                widget.insertItem(target_idx, item_icon, "CADPilot", item_data)
                FreeCAD.Console.PrintMessage("[CADPilot] Reordered after BIM\n")
                return
    except Exception as e:
        FreeCAD.Console.PrintWarning(f"[CADPilot] Reorder failed: {e}\n")


# Delayed UI reorder — run after main window is fully initialised
QtCore.QTimer.singleShot(2000, _reorder_workbench_list)
