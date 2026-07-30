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
