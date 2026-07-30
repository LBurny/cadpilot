import logging
from typing import Any

from mcp.types import ImageContent

from ..freecad_client import FreeCADConnection
from ..guidance import detect_risks, suggest_next_steps
from ..pattern_store import add_pattern, search_patterns
from ..responses import ToolResponse, add_screenshot_if_available, json_response, text_response
from ..session_state import (
    get_current_session,
    list_sessions,
    load_session,
    new_session,
    save_session,
    set_current_session,
)

logger = logging.getLogger("CADPilot")

# Screenshot parameters sent inline with mutation RPCs. The addon resolves
# missing width/height to a downscaled default (see view_manager).
_INLINE_SCREENSHOT = {"view_name": "Isometric"}


def _shot_params(with_screenshot: bool) -> dict[str, Any] | None:
    return _INLINE_SCREENSHOT if with_screenshot else None


def _normalize_object_names(objects: Any) -> list[str]:
    """Extract sorted object names from the addon's ``objects`` fingerprint.

    The addon's ``_run_op_with_screenshot`` returns ``sorted(o.Name for o in
    doc.Objects)`` (a list of plain strings), but ``get_objects`` returns a
    list of dicts (``[{"Name": "Box", ...}]``).  Both shapes may appear in
    RPC results depending on the code path, so normalise defensively.
    """
    if not objects:
        return []
    names: list[str] = []
    for item in objects:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and "Name" in item:
            names.append(item["Name"])
    return sorted(names)


def create_document_operation(
    freecad: FreeCADConnection, name: str, with_screenshot: bool = False
) -> ToolResponse:
    try:
        shot = _shot_params(with_screenshot)
        res = freecad.create_document(name, screenshot=shot)
        if res["success"]:
            response = text_response(f"Document '{res['document_name']}' created successfully")
            return add_screenshot_if_available(response, res.get("screenshot"), not with_screenshot)
        return text_response(f"Failed to create document: {res['error']}")
    except Exception as e:
        logger.error(f"Failed to create document: {e!s}")
        return text_response(f"Failed to create document: {e!s}")


def create_object_operation(
    freecad: FreeCADConnection,
    with_screenshot: bool,
    doc_name: str,
    obj_type: str,
    obj_name: str,
    obj_properties: dict[str, Any] | None = None,
) -> ToolResponse:
    try:
        obj_data = {
            "Name": obj_name,
            "Type": obj_type,
            "Properties": obj_properties or {},
        }
        # The screenshot rides along with the mutation RPC (single round trip,
        # no race with intervening ops); the client falls back to a second
        # get_active_screenshot call against old addons.
        res = freecad.create_object(doc_name, obj_data, screenshot=_shot_params(with_screenshot))
        if res["success"]:
            response = text_response(f"Object '{res['object_name']}' created successfully")
        else:
            return text_response(f"Failed to create object: {res['error']}")
        return add_screenshot_if_available(response, res.get("screenshot"), not with_screenshot)
    except Exception as e:
        logger.error(f"Failed to create object: {e!s}")
        return text_response(f"Failed to create object: {e!s}")


def edit_object_operation(
    freecad: FreeCADConnection,
    with_screenshot: bool,
    doc_name: str,
    obj_name: str,
    obj_properties: dict[str, Any],
) -> ToolResponse:
    try:
        res = freecad.edit_object(
            doc_name,
            obj_name,
            {"Properties": obj_properties},
            screenshot=_shot_params(with_screenshot),
        )
        if res["success"]:
            response = text_response(f"Object '{res['object_name']}' edited successfully")
        else:
            return text_response(f"Failed to edit object: {res['error']}")
        return add_screenshot_if_available(response, res.get("screenshot"), not with_screenshot)
    except Exception as e:
        logger.error(f"Failed to edit object: {e!s}")
        return text_response(f"Failed to edit object: {e!s}")


def delete_object_operation(
    freecad: FreeCADConnection,
    with_screenshot: bool,
    doc_name: str,
    obj_name: str,
) -> ToolResponse:
    try:
        res = freecad.delete_object(doc_name, obj_name, screenshot=_shot_params(with_screenshot))
        if res["success"]:
            response = text_response(f"Object '{res['object_name']}' deleted successfully")
        else:
            return text_response(f"Failed to delete object: {res['error']}")
        return add_screenshot_if_available(response, res.get("screenshot"), not with_screenshot)
    except Exception as e:
        logger.error(f"Failed to delete object: {e!s}")
        return text_response(f"Failed to delete object: {e!s}")


def execute_code_operation(
    freecad: FreeCADConnection,
    with_screenshot: bool,
    code: str,
) -> ToolResponse:
    try:
        res = freecad.execute_code(code, screenshot=_shot_params(with_screenshot))
        if res["success"]:
            # Record as a NON-ATOMIC step: user code may manage its own
            # transactions (or none), so the session cannot guarantee that
            # doc.undo() reverses exactly this step. Rollback past it is
            # blocked unless forced.
            sess = get_current_session()
            step_note = ""
            if sess is not None and sess.status == "active":
                step = sess.add_step(
                    "execute_code",
                    f"execute_code: {code[:80]}",
                    params_summary=code[:200],
                    result_summary=str(res.get("message", ""))[:200],
                    atomic=False,
                )
                save_session(sess)
                step_note = (
                    f" (recorded as non-atomic step #{step.step_number} of session '{sess.name}')"
                )
            response = text_response(f"Code executed successfully: {res['message']}{step_note}")
            return add_screenshot_if_available(response, res.get("screenshot"), not with_screenshot)
        return text_response(f"Failed to execute code: {res['error']}")
    except Exception as e:
        logger.error(f"Failed to execute code: {e!s}")
        return text_response(f"Failed to execute code: {e!s}")


def execute_code_async_operation(
    freecad: FreeCADConnection,
    code: str,
) -> ToolResponse:
    try:
        res = freecad.execute_code_async(code)
        if res["success"]:
            task_id = res.get("task_id")
            hint = (
                f"Poll its status and captured output with get_task_result(task_id='{task_id}')."
                if task_id
                else "This addon version returns no task_id; upgrade the FreeCAD addon to poll results."
            )
            return text_response(
                f"Code execution started in background. Task ID: {task_id or 'unavailable'}.\n"
                f"{hint}\n"
                "Inside the async code, use task_print(...) to capture output for get_task_result."
            )
        return text_response(f"Failed to start async execution: {res.get('error', 'unknown')}")
    except Exception as e:
        logger.error(f"Failed to start async code execution: {e!s}")
        return text_response(f"Failed to start async code execution: {e!s}")


def get_task_result_operation(
    freecad: FreeCADConnection,
    task_id: str,
) -> ToolResponse:
    try:
        res = freecad.get_task_result(task_id)
        if res.get("success"):
            return json_response(res)
        return text_response(f"Failed to get task result: {res.get('error', 'unknown')}")
    except Exception as e:
        logger.error(f"Failed to get task result: {e!s}")
        return text_response(f"Failed to get task result: {e!s}")


def execute_operations_operation(
    freecad: FreeCADConnection,
    with_screenshot: bool,
    doc_name: str,
    ops: list[dict[str, Any]],
    stop_on_error: bool = False,
) -> ToolResponse:
    try:
        res = freecad.execute_operations(
            doc_name,
            ops,
            stop_on_error,
            screenshot=_shot_params(with_screenshot),
        )
        succeeded = sum(1 for r in res.get("results", []) if r.get("success"))
        total = len(res.get("results", []))
        response = json_response(
            {
                "summary": (
                    f"Batch finished: {succeeded}/{total} operations succeeded"
                    + (" (stopped early)" if stop_on_error and succeeded < total else "")
                ),
                **res,
            }
        )
        return add_screenshot_if_available(response, res.get("screenshot"), not with_screenshot)
    except Exception as e:
        logger.error(f"Failed to execute operations: {e!s}")
        hint = (
            " If the error says the method is not found, the FreeCAD addon is too old — "
            "update it to a version with execute_operations support."
        )
        return text_response(f"Failed to execute operations: {e!s}.{hint}")


def get_view_operation(
    freecad: FreeCADConnection,
    view_name: str,
    width: int | None = None,
    height: int | None = None,
    focus_object: str | None = None,
) -> ToolResponse:
    try:
        screenshot = freecad.get_active_screenshot(view_name, width, height, focus_object)
        if screenshot is not None:
            return [ImageContent(type="image", data=screenshot, mimeType="image/png")]
        return text_response(
            "Cannot get screenshot in the current view type (such as TechDraw or Spreadsheet)"
        )
    except Exception as e:
        logger.error(f"Failed to get view: {e!s}")
        return text_response(f"Failed to get view: {e!s}")


def get_objects_operation(
    freecad: FreeCADConnection,
    with_screenshot: bool,
    doc_name: str,
) -> ToolResponse:
    try:
        response = json_response(freecad.get_objects(doc_name))
        screenshot = freecad.get_active_screenshot() if with_screenshot else None
        return add_screenshot_if_available(response, screenshot, not with_screenshot)
    except Exception as e:
        logger.error(f"Failed to get objects: {e!s}")
        return text_response(f"Failed to get objects: {e!s}")


def get_object_operation(
    freecad: FreeCADConnection,
    with_screenshot: bool,
    doc_name: str,
    obj_name: str,
) -> ToolResponse:
    try:
        response = json_response(freecad.get_object(doc_name, obj_name))
        screenshot = freecad.get_active_screenshot() if with_screenshot else None
        return add_screenshot_if_available(response, screenshot, not with_screenshot)
    except Exception as e:
        logger.error(f"Failed to get object: {e!s}")
        return text_response(f"Failed to get object: {e!s}")


def list_documents_operation(freecad: FreeCADConnection) -> ToolResponse:
    return json_response({"success": True, "documents": freecad.list_documents()})


# ============================================================================
# Unified cad() dispatcher + modeling sessions + pattern memory
# ============================================================================

CAD_FEATURE_OPERATIONS = (
    "boolean",
    "fillet",
    "chamfer",
    "loft",
    "sweep",
    "mirror",
    "pattern",
    "move",
    "variables",
    "sketch",
    "pad",
    "pocket",
    "revolution",
    "groove",
    "thickness",
    "draft",
    "datum_plane",
    "hull",
)
# Feature ops whose obj_name names the NEW object (or is unused), not a base.
CAD_NO_BASE_OPERATIONS = ("loft", "sketch", "variables", "datum_plane", "hull")
CAD_OPERATIONS = ("create_object", "edit_object", "delete_object", "batch", *CAD_FEATURE_OPERATIONS)

_AUTO_AUDIT_MAX_OBJECTS = 300
_ISLAND_OBJECTS_PREVIEW = 4


def _format_connectivity_warning(audit: dict[str, Any]) -> str:
    """Format the islands section of a verify_assembly audit as a warning.

    Pure function — returns "" when there is nothing to report or the addon
    did not return an islands section (old addon).
    """
    islands = audit.get("islands")
    if not islands:
        return ""
    count = int(audit.get("summary", {}).get("island_count", len(islands)))
    lines = [f"⚠ Connectivity: {count} disconnected island(s) not touching the main assembly:"]
    for isl in islands[:5]:
        objs = isl.get("objects", [])
        preview = ", ".join(objs[:_ISLAND_OBJECTS_PREVIEW])
        if len(objs) > _ISLAND_OBJECTS_PREVIEW:
            preview += f", +{len(objs) - _ISLAND_OBJECTS_PREVIEW} more"
        gap = isl.get("gap_mm")
        near = isl.get("nearest_main")
        loc = f" — {gap}mm from '{near}'" if gap is not None and near else ""
        lines.append(f"  - [{preview}]{loc}")
    if count > 5:
        lines.append(f"  ... and {count - 5} more island(s)")
    lines.append("Fix gaps so parts touch (≤0.5mm) or intersect before continuing.")
    return "\n" + "\n".join(lines)


def _auto_connectivity_audit(freecad: FreeCADConnection, doc_name: str) -> str:
    """Run a read-only connectivity audit; never raises, never blocks the mutation."""
    try:
        audit = freecad.verify_assembly(doc_name)
        if not audit.get("success"):
            return ""
        if int(audit.get("object_count", 0)) > _AUTO_AUDIT_MAX_OBJECTS:
            return ""
        return _format_connectivity_warning(audit)
    except Exception as e:
        logger.warning(f"auto connectivity audit failed: {e}")
        return ""


def _record_step_if_tracked(
    operation: str,
    doc_name: str,
    description: str,
    default_desc: str,
    params_summary: str,
    summary: str,
    res: dict[str, Any],
) -> str:
    """Record a session step when the active session tracks this document.

    Returns the step-note suffix to append to the tool summary ("" when no
    active session, or a "[not recorded...]" note for a foreign document).
    """
    sess = get_current_session()
    if sess is None or sess.status != "active":
        return ""
    if sess.doc_name != doc_name:
        return f" [not recorded: active session '{sess.name}' tracks document '{sess.doc_name}']"
    step = sess.add_step(
        operation,
        description or default_desc,
        params_summary=params_summary,
        result_summary=summary,
        objects_after=_normalize_object_names(res.get("objects", [])),
        atomic=bool(res.get("transaction", True)),
    )
    save_session(sess)
    return f" [step #{step.step_number} of session '{sess.name}']"


def cad_operation(
    freecad: FreeCADConnection,
    with_screenshot: bool,
    operation: str,
    doc_name: str,
    *,
    obj_type: str | None = None,
    obj_name: str | None = None,
    obj_properties: dict[str, Any] | None = None,
    ops: list[dict[str, Any]] | None = None,
    stop_on_error: bool = False,
    description: str = "",
    auto_audit: bool = True,
) -> ToolResponse:
    """Unified mutation entry point (nsforge math()-style dispatcher).

    Records a session step when the active session tracks this document and
    a transaction was committed. A partially successful batch also counts —
    the addon commits whenever at least one op succeeded, and the step log
    must stay in sync with the undo stack.
    """
    shot = _shot_params(with_screenshot)
    batch_succeeded = 0
    try:
        if operation == "create_object":
            if not obj_type or not obj_name:
                return text_response("create_object requires obj_type and obj_name")
            obj_data = {
                "Name": obj_name,
                "Type": obj_type,
                "Properties": obj_properties or {},
            }
            res = freecad.create_object(doc_name, obj_data, screenshot=shot)
            success = bool(res.get("success"))
            summary = (
                f"Object '{res['object_name']}' created successfully"
                if success
                else f"Failed to create object: {res.get('error')}"
            )
            params_summary = f"{obj_type} '{obj_name}'"
            if obj_properties and "Placement" in obj_properties:
                params_summary += " +Placement"  # marker for guidance.detect_risks
            default_desc = f"create {obj_type} '{obj_name}'"
        elif operation == "edit_object":
            if not obj_name:
                return text_response("edit_object requires obj_name")
            res = freecad.edit_object(
                doc_name, obj_name, {"Properties": obj_properties or {}}, screenshot=shot
            )
            success = bool(res.get("success"))
            summary = (
                f"Object '{res['object_name']}' edited successfully"
                if success
                else f"Failed to edit object: {res.get('error')}"
            )
            params_summary = f"'{obj_name}' props={list((obj_properties or {}).keys())}"
            if obj_properties and "Placement" in obj_properties:
                params_summary += " +Placement"  # marker for guidance.detect_risks
            default_desc = f"edit '{obj_name}'"
        elif operation == "delete_object":
            if not obj_name:
                return text_response("delete_object requires obj_name")
            res = freecad.delete_object(doc_name, obj_name, screenshot=shot)
            success = bool(res.get("success"))
            summary = (
                f"Object '{res['object_name']}' deleted successfully"
                if success
                else f"Failed to delete object: {res.get('error')}"
            )
            params_summary = f"'{obj_name}'"
            default_desc = f"delete '{obj_name}'"
        elif operation in CAD_FEATURE_OPERATIONS:
            if not obj_name and operation not in CAD_NO_BASE_OPERATIONS:
                return text_response(f"{operation} requires obj_name (the base object)")
            params = dict(obj_properties or {})
            spec = {"type": operation, "base": obj_name, **params}
            res = freecad.create_feature(doc_name, spec, screenshot=shot)
            success = bool(res.get("success"))
            summary = (
                f"{operation} '{res['object_name']}' created successfully"
                if success
                else f"Failed to create {operation}: {res.get('error')}"
            )
            params_summary = f"on '{obj_name}' {list(params.keys())}"
            default_desc = f"{operation} on '{obj_name}'"
        elif operation == "batch":
            if not ops:
                return text_response("batch requires a non-empty ops list")
            res = freecad.execute_operations(doc_name, ops, stop_on_error, screenshot=shot)
            results = res.get("results", [])
            batch_succeeded = sum(1 for r in results if r.get("success"))
            success = bool(res.get("success"))
            summary = f"Batch finished: {batch_succeeded}/{len(results)} operations succeeded"
            params_summary = f"{len(ops)} ops"
            default_desc = f"batch of {len(ops)} ops"
        else:
            return text_response(
                f"Unknown cad operation '{operation}'. Supported: {', '.join(CAD_OPERATIONS)}"
            )
    except Exception as e:
        logger.error(f"cad {operation} failed: {e!s}")
        return text_response(f"cad {operation} failed: {e!s}")

    committed = success or (operation == "batch" and batch_succeeded > 0)
    step_note = ""
    audit_note = ""
    if committed:
        if auto_audit:
            audit_note = _auto_connectivity_audit(freecad, doc_name)
        summary += audit_note
        step_note = _record_step_if_tracked(
            operation,
            doc_name,
            description,
            default_desc,
            params_summary,
            summary,
            res,
        )

    if operation == "batch":
        response = json_response({"summary": summary + step_note, **res})
    elif success:
        response = text_response(summary + step_note)
    else:
        return text_response(summary)
    return add_screenshot_if_available(response, res.get("screenshot"), not with_screenshot)


# --- modeling sessions --------------------------------------------------------


def session_start_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    name: str = "",
    create_document: bool = False,
) -> ToolResponse:
    try:
        if create_document and doc_name not in freecad.list_documents():
            res = freecad.create_document(doc_name)
            if not res.get("success"):
                return text_response(f"Failed to create document: {res.get('error')}")
            doc_name = res["document_name"]  # FreeCAD may sanitize the name
    except Exception as e:
        return text_response(f"Failed to prepare document: {e!s}")
    sess = new_session(name or f"Modeling {doc_name}", doc_name)
    set_current_session(sess)
    save_session(sess)
    return json_response(
        {
            "success": True,
            "session_id": sess.session_id,
            "name": sess.name,
            "doc_name": sess.doc_name,
            "message": f"Session started. Mutations via cad() on '{doc_name}' are now "
            "recorded as steps; use session_rollback to backtrack.",
        }
    )


def _require_session() -> tuple[Any | None, ToolResponse | None]:
    sess = get_current_session()
    if sess is None:
        return None, text_response("No active session. Use session_start or session_resume first.")
    return sess, None


def session_status_operation(freecad: FreeCADConnection) -> ToolResponse:
    sess, err = _require_session()
    if err:
        return err
    doc_open = False
    object_names: list[str] | None = None
    try:
        doc_open = sess.doc_name in freecad.list_documents()
        if doc_open:
            object_names = sorted(o["Name"] for o in freecad.get_objects(sess.doc_name))
    except Exception as e:
        logger.warning(f"session_status: cannot query document state: {e}")
    suggestions = suggest_next_steps(sess, object_names or [])
    risks = detect_risks(sess, doc_open, object_names)

    lines = [
        f"📊 **{sess.name}** (doc `{sess.doc_name}`, {sess.step_count} steps, {sess.status})",
    ]
    if suggestions:
        lines.append("\n💡 **Next steps:**")
        lines.extend(f"- `{s['tool']}` {s['operation']}: {s['reason']}" for s in suggestions)
    if risks:
        lines.append("\n⚠️ **Risks:**")
        lines.extend(f"- {r['message']}" for r in risks)
    return json_response(
        {
            "success": True,
            "session_id": sess.session_id,
            "name": sess.name,
            "doc_name": sess.doc_name,
            "status": sess.status,
            "step_count": sess.step_count,
            "redo_available": len(sess.redo_buffer),
            "document_open": doc_open,
            "object_count": len(object_names or []),
            "next_steps": suggestions,
            "risks": risks,
            "display_text": "\n".join(lines),
        }
    )


def session_get_steps_operation() -> ToolResponse:
    sess, err = _require_session()
    if err:
        return err
    return json_response(
        {
            "success": True,
            "session_id": sess.session_id,
            "steps": [s.to_dict() for s in sess.steps],
            "notes": sess.notes,
            "redo_buffer": [s.to_dict() for s in sess.redo_buffer],
            "count": sess.step_count,
        }
    )


def session_rollback_operation(
    freecad: FreeCADConnection,
    to_step: int,
    force: bool = False,
) -> ToolResponse:
    sess, err = _require_session()
    if err:
        return err
    if to_step < 0 or to_step > sess.step_count:
        return text_response(
            f"Invalid step {to_step}. Valid range: 0-{sess.step_count} (0 = undo all steps)."
        )
    if to_step == sess.step_count:
        return text_response("Already at this step; nothing to roll back.")
    blocking = sess.non_atomic_steps_after(to_step)
    if blocking and not force:
        return text_response(
            f"Cannot roll back past step(s) {blocking}: they were made via execute_code "
            "without a transaction, so undo would revert the wrong change. "
            "Pass force=True to roll back anyway (at your own risk)."
        )
    n = sess.step_count - to_step
    try:
        res = freecad.undo_transactions(sess.doc_name, n)
    except Exception as e:
        return text_response(f"Rollback failed: {e!s}")
    if not res.get("success"):
        return text_response(f"Rollback failed in FreeCAD: {res.get('error')}")

    undone = res.get("count", n)
    warnings = []
    if undone < n:
        warnings.append(
            f"Only {undone}/{n} transactions could be undone (undo stack was shorter "
            "than the session log — external GUI edits?); the log was truncated to match."
        )
    removed = sess.truncate_to(sess.step_count - undone)
    save_session(sess)

    state_matches = None
    if sess.steps:
        current_names = _normalize_object_names(res.get("objects", []))
        state_matches = current_names == sess.steps[-1].objects_after
        if not state_matches:
            warnings.append(
                "Post-rollback object list differs from the recorded step fingerprint — "
                "the document was likely edited outside cad()."
            )
    return json_response(
        {
            "success": True,
            "rolled_back_to": sess.step_count,
            "undone_transactions": undone,
            "removed_steps": [s.step_number for s in removed],
            "state_matches_log": state_matches,
            "objects": res.get("objects", []),
            "warnings": warnings,
            "display_text": (
                f"⏪ Rolled back {undone} step(s) to step {sess.step_count}. "
                f"Removed: {[s.step_number for s in removed]}."
                + (" ⚠️ " + " ".join(warnings) if warnings else "")
            ),
        }
    )


def session_redo_operation(freecad: FreeCADConnection, n: int = 1) -> ToolResponse:
    sess, err = _require_session()
    if err:
        return err
    if not sess.redo_buffer:
        return text_response("Nothing to redo (no previously rolled-back steps).")
    n = max(1, min(int(n), len(sess.redo_buffer)))
    try:
        res = freecad.redo_transactions(sess.doc_name, n)
    except Exception as e:
        return text_response(f"Redo failed: {e!s}")
    if not res.get("success"):
        return text_response(f"Redo failed in FreeCAD: {res.get('error')}")
    restored = sess.restore_steps(res.get("count", 0))
    save_session(sess)
    return json_response(
        {
            "success": True,
            "restored_steps": [s.step_number for s in restored],
            "step_count": sess.step_count,
            "redo_remaining": len(sess.redo_buffer),
            "objects": res.get("objects", []),
        }
    )


def session_add_note_operation(note: str, note_type: str = "observation") -> ToolResponse:
    sess, err = _require_session()
    if err:
        return err
    entry = sess.add_note(note, note_type)
    save_session(sess)
    return json_response({"success": True, "note": entry})


def session_pause_operation() -> ToolResponse:
    sess, err = _require_session()
    if err:
        return err
    sess.status = "paused"
    save_session(sess)
    set_current_session(None)
    return json_response(
        {
            "success": True,
            "session_id": sess.session_id,
            "message": f"Session '{sess.name}' paused and saved. Resume with session_resume('{sess.session_id}').",
        }
    )


def session_resume_operation(freecad: FreeCADConnection, session_id: str) -> ToolResponse:
    sess = load_session(session_id)
    if sess is None:
        return json_response(
            {
                "success": False,
                "error": f"Session '{session_id}' not found",
                "available_sessions": [s["session_id"] for s in list_sessions()],
            }
        )
    sess.status = "active"
    set_current_session(sess)
    save_session(sess)
    warning = ""
    try:
        if sess.doc_name not in freecad.list_documents():
            warning = (
                f" Warning: document '{sess.doc_name}' is not open in FreeCAD; "
                "mutations and rollback will fail until it is reopened."
            )
    except Exception:
        pass
    return json_response(
        {
            "success": True,
            "session_id": sess.session_id,
            "name": sess.name,
            "doc_name": sess.doc_name,
            "step_count": sess.step_count,
            "message": f"Session resumed.{warning}",
        }
    )


def session_list_operation() -> ToolResponse:
    sess = get_current_session()
    return json_response(
        {
            "success": True,
            "current_session_id": sess.session_id if sess else None,
            "sessions": list_sessions(),
        }
    )


def session_complete_operation(
    freecad: FreeCADConnection,
    save: bool = False,
    save_path: str | None = None,
    description: str = "",
    tags: list[str] | None = None,
) -> ToolResponse:
    sess, err = _require_session()
    if err:
        return err
    saved_file = None
    save_warning = None
    if save:
        try:
            res = freecad.save_document(sess.doc_name, save_path)
            if res.get("success"):
                saved_file = res.get("file_name")
            else:
                save_warning = res.get("error")
        except Exception as e:
            save_warning = str(e)

    pattern = add_pattern(
        name=sess.name,
        description=description or f"Modeling workflow for document '{sess.doc_name}'",
        steps=[f"{s.step_number}. {s.operation} — {s.description}" for s in sess.steps],
        tags=tags,
        source="session",
    )
    sess.status = "completed"
    save_session(sess)
    set_current_session(None)
    return json_response(
        {
            "success": True,
            "session_id": sess.session_id,
            "steps_recorded": sess.step_count,
            "pattern_id": pattern["pattern_id"],
            "saved_file": saved_file,
            "save_warning": save_warning,
            "message": f"Session completed; workflow stored as pattern '{pattern['pattern_id']}'. "
            "Recall it later with recall_patterns().",
        }
    )


# --- pattern memory ------------------------------------------------------------


def save_pattern_operation(
    name: str,
    description: str,
    code: str = "",
    tags: list[str] | None = None,
) -> ToolResponse:
    entry = add_pattern(name=name, description=description, code=code, tags=tags, source="manual")
    return json_response(
        {
            "success": True,
            "pattern_id": entry["pattern_id"],
            "message": f"Pattern '{name}' stored.",
        }
    )


def recall_patterns_operation(query: str, limit: int = 3) -> ToolResponse:
    found = search_patterns(query, limit=max(1, int(limit)))
    if not found:
        return text_response(
            f"No patterns match '{query}'. The store is empty or unrelated — "
            "rely on your own knowledge, or use inspect_freecad for API details."
        )
    return json_response({"success": True, "count": len(found), "patterns": found})


# --- on-demand reference docs (prompt-explosion fix) ---------------------------


def operation_help_operation(operation: str | None = None) -> ToolResponse:
    from ..tool_docs import operation_help_text

    return text_response(operation_help_text(operation))


# --- runtime introspection ------------------------------------------------------


def inspect_freecad_operation(
    freecad: FreeCADConnection,
    doc_name: str | None = None,
    obj_name: str | None = None,
    dotted_name: str | None = None,
) -> ToolResponse:
    try:
        res = freecad.inspect_freecad(doc_name, obj_name, dotted_name)
    except Exception as e:
        return text_response(f"Failed to inspect: {e!s}")
    if res.get("success"):
        return json_response(res)
    return text_response(f"Inspection failed: {res.get('error')}")


# --- geometry sensing (read-only) ---------------------------------------------


def measure_geometry_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    obj_name: str,
) -> ToolResponse:
    try:
        return json_response(freecad.measure_geometry(doc_name, obj_name))
    except Exception as e:
        logger.error(f"Failed to measure geometry: {e!s}")
        return text_response(f"Failed to measure geometry: {e!s}")


def get_topology_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    obj_name: str,
    element: str = "faces",
    limit: int = 50,
    offset: int = 0,
) -> ToolResponse:
    try:
        return json_response(freecad.get_topology(doc_name, obj_name, element, limit, offset))
    except Exception as e:
        logger.error(f"Failed to get topology: {e!s}")
        return text_response(f"Failed to get topology: {e!s}")


def check_interference_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    obj_a: str,
    obj_b: str,
) -> ToolResponse:
    try:
        return json_response(freecad.check_interference(doc_name, obj_a, obj_b))
    except Exception as e:
        logger.error(f"Failed to check interference: {e!s}")
        return text_response(f"Failed to check interference: {e!s}")


def get_positioning_info_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    obj_name: str,
    element: str,
    element_index: int,
) -> ToolResponse:
    try:
        return json_response(
            freecad.get_positioning_info(doc_name, obj_name, element, element_index)
        )
    except Exception as e:
        logger.error(f"Failed to get positioning info: {e!s}")
        return text_response(f"Failed to get positioning info: {e!s}")


def align_shapes_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    obj_name: str,
    element: str,
    element_index: int,
    target_obj_name: str,
    target_element: str,
    target_element_index: int,
    mode: str = "touch",
    offset: float = 0.0,
) -> ToolResponse:
    try:
        res = freecad.align_shapes(
            doc_name,
            obj_name,
            element,
            element_index,
            target_obj_name,
            target_element,
            target_element_index,
            mode,
            offset,
        )
    except Exception as e:
        logger.error(f"Failed to align shapes: {e!s}")
        return text_response(f"Failed to align shapes: {e!s}")
    # align_shapes commits a document transaction on the addon side, so it
    # MUST be recorded as a session step — otherwise session_rollback would
    # undo this unrecorded transaction while truncating a different step,
    # desyncing the step log from the undo stack.
    if res.get("success") and res.get("transaction"):
        step_note = _record_step_if_tracked(
            "align_shapes",
            doc_name,
            "",
            f"align '{obj_name}' -> '{target_obj_name}' ({mode})",
            f"'{obj_name}' {element}[{element_index}] {mode} -> "
            f"'{target_obj_name}' {target_element}[{target_element_index}]",
            f"aligned '{obj_name}' to '{target_obj_name}' ({mode})",
            res,
        )
        if step_note:
            res = {**res, "step_note": step_note}
    return json_response(res)


# --- assembly toolchain (anchors / assemble / verify) -------------------------


def get_anchors_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    obj_name: str,
) -> ToolResponse:
    try:
        return json_response(freecad.get_anchors(doc_name, obj_name))
    except Exception as e:
        logger.error(f"Failed to get anchors: {e!s}")
        return text_response(f"Failed to get anchors: {e!s}")


def set_anchors_operation(
    freecad: FreeCADConnection,
    with_screenshot: bool,
    doc_name: str,
    obj_name: str,
    anchors: dict[str, Any],
    replace: bool = False,
    coord_frame: str = "local",
) -> ToolResponse:
    if not anchors:
        return text_response("set_anchors requires a non-empty anchors dict")
    try:
        res = freecad.set_anchors(
            doc_name,
            obj_name,
            anchors,
            replace,
            coord_frame,
            screenshot=_shot_params(with_screenshot),
        )
    except Exception as e:
        logger.error(f"Failed to set anchors: {e!s}")
        return text_response(f"Failed to set anchors: {e!s}")
    summary = (
        f"Set {res.get('anchor_count', len(anchors))} anchor(s) on '{obj_name}'"
        if res.get("success")
        else f"Failed to set anchors: {res.get('error')}"
    )
    if res.get("success"):
        summary += _record_step_if_tracked(
            "set_anchors",
            doc_name,
            "",
            f"set_anchors on '{obj_name}'",
            f"'{obj_name}' anchors={list(anchors.keys())}",
            summary,
            res,
        )
    response = json_response({"summary": summary, **res})
    return add_screenshot_if_available(response, res.get("screenshot"), not with_screenshot)


def assemble_operation(
    freecad: FreeCADConnection,
    with_screenshot: bool,
    doc_name: str,
    mates: list[dict[str, Any]],
    tolerance: float = 0.1,
    stop_on_error: bool = True,
) -> ToolResponse:
    if not mates:
        return text_response("assemble requires a non-empty mates list")
    try:
        res = freecad.assemble(
            doc_name,
            mates,
            tolerance,
            stop_on_error,
            screenshot=_shot_params(with_screenshot),
        )
    except Exception as e:
        logger.error(f"Failed to assemble: {e!s}")
        return text_response(f"Failed to assemble: {e!s}")
    committed = bool(res.get("transaction")) and res.get("passed", 0) > 0
    summary = (
        f"Assembled {res.get('passed', 0)}/{len(mates)} mates"
        f" (tolerance {tolerance}mm, {res.get('failed', 0)} failed)"
    )
    if committed:
        summary += _record_step_if_tracked(
            "assemble",
            doc_name,
            "",
            f"assemble {len(mates)} mates",
            f"{len(mates)} mates tol={tolerance}",
            summary,
            res,
        )
    response = json_response({"summary": summary, **res})
    return add_screenshot_if_available(response, res.get("screenshot"), not with_screenshot)


def verify_assembly_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    checks: list[dict[str, Any]] | None = None,
    float_threshold: float = 1.0,
    interference_min_volume: float = 1.0,
) -> ToolResponse:
    try:
        return json_response(
            freecad.verify_assembly(doc_name, checks, float_threshold, interference_min_volume)
        )
    except Exception as e:
        logger.error(f"Failed to verify assembly: {e!s}")
        return text_response(f"Failed to verify assembly: {e!s}")
