"""Active-view orientation, sizing, and screenshot capture."""

from typing import Any

import FreeCAD
import FreeCADGui

from rpc_server.gui_dispatch import _flush_gui_events

_VIEW_DISPATCH = {
    "Isometric": "viewIsometric",
    "Front": "viewFront",
    "Top": "viewTop",
    "Right": "viewRight",
    "Back": "viewBack",
    "Left": "viewLeft",
    "Bottom": "viewBottom",
    "Dimetric": "viewDimetric",
    "Trimetric": "viewTrimetric",
}


def _get_view_size(view: Any) -> tuple[int, int]:
    try:
        size = view.getSize()
        if isinstance(size, (list, tuple)) and len(size) >= 2:
            return max(1, int(size[0])), max(1, int(size[1]))
        return max(1, int(size.width())), max(1, int(size.height()))
    except Exception:
        return 1024, 768


# Default cap applied when the caller does not request an explicit size:
# the long edge is scaled down to this many pixels. Full-viewport PNGs
# base64-encode to hundreds of KB, and every pixel is paid as tokens by
# the LLM client. Callers can always override with explicit width/height.
DEFAULT_MAX_DIM = 384


def _resolve_screenshot_size(
    view: Any,
    width: int | None,
    height: int | None,
) -> tuple[int, int]:
    view_width, view_height = _get_view_size(view)
    if width is None and height is None:
        long_edge = max(view_width, view_height)
        if long_edge > DEFAULT_MAX_DIM:
            scale = DEFAULT_MAX_DIM / long_edge
            return max(1, round(view_width * scale)), max(1, round(view_height * scale))
        return view_width, view_height
    resolved_width = view_width if width is None else max(1, int(width))
    resolved_height = view_height if height is None else max(1, int(height))
    return resolved_width, resolved_height


_STD_COMMAND_DISPATCH = {
    "Isometric": "Std_ViewIsometric",
    "Front": "Std_ViewFront",
    "Top": "Std_ViewTop",
    "Right": "Std_ViewRight",
    "Back": "Std_ViewRear",
    "Left": "Std_ViewLeft",
    "Bottom": "Std_ViewBottom",
    "Dimetric": "Std_ViewDimetric",
    "Trimetric": "Std_ViewTrimetric",
}


def apply_view_orientation(view: Any, view_name: str) -> None:
    method_name = _VIEW_DISPATCH.get(view_name)
    if method_name is None:
        raise ValueError(f"Invalid view name: {view_name}")
    if hasattr(view, method_name):
        getattr(view, method_name)()
    else:
        # Fallback for views that lack the direct Python method
        # (e.g. some FreeCAD versions / view types)
        cmd = _STD_COMMAND_DISPATCH.get(view_name)
        if cmd:
            FreeCADGui.runCommand(cmd)
        else:
            FreeCAD.Console.PrintWarning(
                f"apply_view_orientation: no method or command for '{view_name}'\n"
            )


def _send_viewselection(gdoc) -> None:
    """Frame the selection in the RIGHT view: without a binding the foreground
    view; with one, the bound document's own views — ``SendMsgToActiveView``
    would hit whatever tab a concurrent agent holds in front."""
    if gdoc is not None:
        gdoc.sendMsgToViews("ViewSelection")
    else:
        FreeCADGui.SendMsgToActiveView("ViewSelection")


def save_active_screenshot(
    save_path: str,
    view_name: str = "Isometric",
    width: int | None = None,
    height: int | None = None,
    focus_object: str | None = None,
    doc_name: str | None = None,
):
    """Save a PNG of the DOCUMENT's active view to ``save_path``.

    With ``doc_name`` the capture targets THAT document's view; without it,
    the foreground view. saveImage() renders OFFSCREEN, so a background
    document (another agent's foreground tab) captures without stealing MDI
    focus — no tab flip, the other agent never notices. ``Gui activation``
    is deliberately absent: FreeCADGui.activateDocument does not even exist
    on 1.1.x, and flipping the foreground tab would yank the MDI focus out
    from under a concurrent agent.

    Returns ``True`` on success, or an error string on failure (preserves the
    legacy GUI-handler return contract).
    """
    try:
        if doc_name:
            doc = FreeCAD.getDocument(doc_name)
            gdoc = FreeCADGui.getDocument(doc_name)
            view = gdoc.activeView()
        else:
            # The focus lookup must search the document the captured VIEW
            # shows (the GUI foreground), not App.ActiveDocument: bound calls
            # keep flipping the latter under a concurrent agent.
            gdoc = FreeCADGui.ActiveDocument
            view = gdoc.ActiveView
            doc = gdoc.Document
        if not hasattr(view, "saveImage"):
            return "Current view does not support screenshots"

        apply_view_orientation(view, view_name)

        focused_selection = False
        # The resolved object we frame on (when focus_object is given), kept so
        # the framing can be re-applied synchronously right before saveImage().
        focus_target = None
        # The user's (or another agent's) selection is process-global GUI
        # state; the focus path used to clearSelection() it away and leave
        # nothing behind. Save it and hand it back after the capture.
        prior_selection = list(FreeCADGui.Selection.getSelection())

        if focus_object:
            obj = doc.getObject(focus_object) if doc else None
            if obj:
                FreeCADGui.Selection.clearSelection()
                FreeCADGui.Selection.addSelection(obj)
                _send_viewselection(gdoc)
                focused_selection = True
                focus_target = obj
                _flush_gui_events()
                FreeCADGui.Selection.clearSelection()
            else:
                view.fitAll()
        else:
            view.fitAll()

        _flush_gui_events()
        # On macOS, when the FreeCAD window is not exposed (fully occluded or
        # minimized), saveImage() right after pumping the event loop grabs a blank
        # frame. Re-issuing the framing synchronously forces a redraw first. The
        # flush above is kept intentionally — Linux needs it for the stale-frame
        # fix (#51/#53).
        if focused_selection and focus_target is not None:
            FreeCADGui.Selection.addSelection(focus_target)
            _send_viewselection(gdoc)
        else:
            view.fitAll()
        resolved_width, resolved_height = _resolve_screenshot_size(view, width, height)
        view.saveImage(save_path, resolved_width, resolved_height, "Current")

        if focused_selection:
            # Hand the selection back: the focus framing cleared the caller's
            # (or another agent's) process-global selection, and the old
            # post-saveImage clearSelection() then left NOTHING selected.
            # Restore exactly what was selected before the capture.
            FreeCADGui.Selection.clearSelection()
            for sel in prior_selection:
                FreeCADGui.Selection.addSelection(sel)
            _flush_gui_events(delay_ms=0)
        return True
    except Exception as e:
        return str(e)
