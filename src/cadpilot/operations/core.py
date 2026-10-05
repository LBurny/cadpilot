import logging
from typing import Any

from ..freecad_client import FreeCADConnection
from ..guidance import detect_risks, suggest_next_steps
from ..pattern_store import add_pattern, search_patterns
from ..responses import (
    ToolResponse,
    json_response,
    screenshot_content,
    text_response,
)
from ..session_state import (
    get_current_session,
    list_sessions,
    load_session,
    new_session,
    save_session,
    set_current_session,
)

logger = logging.getLogger("CADPilot")


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


# Per-process home document: the document THIS server process last created or
# explicitly mutated. Every MCP client spawns its own server process, so two
# concurrent agents each keep their own. An execute_code that names no document
# and has no active session binds here instead of FreeCAD's global active
# document, which under two agents is whichever document the OTHER agent's
# call left active (the "steps pile into each other's journals" bug).
_last_doc_name: str | None = None


def get_last_doc_name() -> str | None:
    return _last_doc_name


def reset_last_doc_name() -> None:
    global _last_doc_name
    _last_doc_name = None


def _set_last_doc(doc_name: str | None) -> None:
    global _last_doc_name
    if doc_name:
        _last_doc_name = doc_name


def create_document_operation(
    freecad: FreeCADConnection,
    name: str,
) -> ToolResponse:
    try:
        res = freecad.create_document(name)
        if res["success"]:
            _set_last_doc(res["document_name"])
            return text_response(f"Document '{res['document_name']}' created successfully")
        return text_response(f"Failed to create document: {res['error']}")
    except Exception as e:
        logger.error(f"Failed to create document: {e!s}")
        return text_response(f"Failed to create document: {e!s}")


def create_object_operation(
    freecad: FreeCADConnection,
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
        res = freecad.create_object(doc_name, obj_data)
        if res["success"]:
            return text_response(f"Object '{res['object_name']}' created successfully")
        return text_response(f"Failed to create object: {res['error']}")
    except Exception as e:
        logger.error(f"Failed to create object: {e!s}")
        return text_response(f"Failed to create object: {e!s}")


def edit_object_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    obj_name: str,
    obj_properties: dict[str, Any],
) -> ToolResponse:
    try:
        res = freecad.edit_object(
            doc_name,
            obj_name,
            {"Properties": obj_properties},
        )
        if res["success"]:
            return text_response(f"Object '{res['object_name']}' edited successfully")
        return text_response(f"Failed to edit object: {res['error']}")
    except Exception as e:
        logger.error(f"Failed to edit object: {e!s}")
        return text_response(f"Failed to edit object: {e!s}")


def delete_object_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    obj_name: str,
) -> ToolResponse:
    try:
        res = freecad.delete_object(doc_name, obj_name)
        if res["success"]:
            return text_response(f"Object '{res['object_name']}' deleted successfully")
        return text_response(f"Failed to delete object: {res['error']}")
    except Exception as e:
        logger.error(f"Failed to delete object: {e!s}")
        return text_response(f"Failed to delete object: {e!s}")


def execute_code_operation(
    freecad: FreeCADConnection,
    code: str,
    doc_name: str | None = None,
) -> ToolResponse:
    # Bind the run to ONE document up front: under two concurrent agents the
    # active document is whichever the OTHER agent's call left active, so an
    # unbound run opens its transaction and files its journal step there. An
    # explicit doc_name wins; then an active session binds automatically (the
    # modeling flow never has to pass anything); then this process's home
    # document (what it last created or mutated) — each agent runs its own
    # server process, so two agents stick to their own document even when
    # neither passes anything. Only a fresh process that touched nothing
    # still falls through to the global active document.
    sess = get_current_session()
    if doc_name is None and sess is not None and sess.status == "active":
        doc_name = sess.doc_name
    home_bound = False
    if doc_name is None and _last_doc_name is not None:
        doc_name = _last_doc_name
        home_bound = True
    bind_note = (
        f" (no doc_name given: bound to '{doc_name}', the document this server "
        "last created or mutated — pass doc_name to target another document)"
        if home_bound
        else ""
    )
    try:
        res = freecad.execute_code(code, doc_name=doc_name)
        _set_last_doc(doc_name)
        if res["success"]:
            # The addon wraps the snippet in a FreeCAD transaction; ``changed``
            # says whether it produced an undo entry. A mutating snippet is an
            # ATOMIC step — rollback/replay treat it like any other cad() step.
            # A read-only run is NOT recorded: it owns no transaction, and a
            # session step it cannot undo would break the log's one-transaction-
            # per-step invariant (and block rollback). Older addons send no
            # flag; treat that as read-only, the conservative default.
            changed = bool(res.get("changed"))
            # The addon reports the document that ACTUALLY changed and whether
            # that change was inside the transaction it opened for us. A snippet
            # may switch documents mid-run (a documented move), so neither can be
            # assumed from the argument list — assuming it made a foreign change
            # read as "no document change" AND filed the step in the wrong log.
            attributed = bool(res.get("attributed", True))
            foreign = [n for n in (res.get("foreign_changes") or []) if n]
            step_note = ""
            if changed and attributed and sess is not None and sess.status == "active":
                # A session step must own a transaction on the SESSION's
                # document — a snippet that mutated another document (its undo
                # entry lives on that document's stack) must never be recorded
                # here, or session_rollback pops the wrong document's
                # transactions and silently reports success.
                changed_doc = res.get("document")
                if changed_doc is None:
                    step_note = (
                        " (changed a document, but the addon did not report which one — "
                        "not recorded; update the FreeCAD addon for session tracking)"
                    )
                elif changed_doc != sess.doc_name:
                    step_note = (
                        f" (changed document '{changed_doc}' — not part of session "
                        f"'{sess.name}' (bound to '{sess.doc_name}'); not recorded. "
                        "It is a journal step on that document: roll it back with "
                        "step_control(doc_name=…) instead)"
                    )
                else:
                    step = sess.add_step(
                        "execute_code",
                        f"execute_code: {code[:80]}",
                        params_summary=code[:200],
                        result_summary=str(res.get("message", ""))[:200],
                        atomic=True,
                    )
                    save_session(sess)
                    step_note = (
                        f" (recorded as atomic step #{step.step_number} of session '{sess.name}')"
                    )
            elif changed and not attributed:
                step_note = (
                    " (a transaction was already open when the snippet ran, so its change "
                    "cannot be attributed to a step of ours — not recorded)"
                )
            elif not changed:
                step_note = " (read-only: no document change, not recorded as a step)"
            if foreign:
                step_note += (
                    " WARNING: the snippet also changed "
                    + ", ".join(f"'{n}'" for n in foreign)
                    + " — that change happened outside this document's transaction, so it is "
                    "not part of any session step (it owns its own undo entry on that document)"
                )
            return text_response(
                f"Code executed successfully: {res['message']}{step_note}{bind_note}"
            )
        return text_response(f"Failed to execute code: {res['error']}{bind_note}")
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
    doc_name: str,
    ops: list[dict[str, Any]],
    stop_on_error: bool = False,
) -> ToolResponse:
    try:
        res = freecad.execute_operations(
            doc_name,
            ops,
            stop_on_error,
        )
        succeeded = sum(1 for r in res.get("results", []) if r.get("success"))
        total = len(res.get("results", []))
        return json_response(
            {
                "summary": (
                    f"Batch finished: {succeeded}/{total} operations succeeded"
                    + (" (stopped early)" if stop_on_error and succeeded < total else "")
                    + _BATCH_COMMIT_NOTE
                ),
                **res,
            }
        )
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
    doc_name: str | None = None,
) -> ToolResponse:
    try:
        screenshot = freecad.get_active_screenshot(
            view_name, width, height, focus_object, doc_name=doc_name
        )
        if screenshot is not None:
            return [screenshot_content(screenshot)]
        reason = ""
        try:
            reason = str(freecad.get_last_screenshot_error() or "")
        except Exception:
            # Old addon without the diagnostic RPC (or a dead connection): fall
            # back to the generic explanation instead of turning a failed
            # capture into a confusing secondary error.
            reason = ""
        if reason:
            return text_response(
                f"Cannot capture the current view: {reason}. "
                "If the FreeCAD window is occluded or minimized, or the model just "
                "changed, retry with focus_object=<object name>: framing the object "
                "skips the automatic fit and forces a repaint."
            )
        return text_response(
            "Cannot capture the current view (the view type may not support screenshots, "
            "or the capture failed). Retry with focus_object=<object name>, which forces "
            "a repaint first."
        )
    except Exception as e:
        logger.error(f"Failed to get view: {e!s}")
        return text_response(f"Failed to get view: {e!s}")


def _page_envelope(items: list[Any], limit: int, offset: int, *, key: str) -> dict[str, Any]:
    """Spec-shaped page: items under *key* plus total/count/offset/has_more.

    mcp-builder pagination guidance (total, count, offset, items, has_more,
    next_offset). The item list stays under its domain key ("objects",
    "documents", "steps") so existing consumers keep working.
    """
    offset = max(0, offset)
    limit = max(1, limit)
    page = items[offset : offset + limit]
    consumed = offset + len(page)
    has_more = consumed < len(items)
    return {
        key: page,
        "total": len(items),
        "count": len(page),
        "offset": offset,
        "has_more": has_more,
        "next_offset": consumed if has_more else None,
    }


def get_objects_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    obj_name: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ToolResponse:
    try:
        if obj_name is not None:
            return json_response(freecad.get_object(doc_name, obj_name))
        objects = freecad.get_objects(doc_name)
        objects = objects if isinstance(objects, list) else []
        return json_response(
            {"success": True, **_page_envelope(objects, limit, offset, key="objects")}
        )
    except Exception as e:
        target = f"object '{obj_name}'" if obj_name is not None else "objects"
        logger.error(f"Failed to get {target}: {e!s}")
        return text_response(f"Failed to get {target}: {e!s}")


def list_documents_operation(
    freecad: FreeCADConnection, limit: int = 50, offset: int = 0
) -> ToolResponse:
    documents = freecad.list_documents()
    documents = documents if isinstance(documents, list) else []
    return json_response(
        {"success": True, **_page_envelope(documents, limit, offset, key="documents")}
    )


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
    "color",
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

# Result keys that are transport bookkeeping rather than findings: `objects` is
# the whole document's object-name fingerprint (hundreds of names on a real
# model), `screenshot` may still arrive from an old addon's mutation reply, and
# success/object_name are already spelled out in the summary text.
_RESULT_BOOKKEEPING = frozenset({"success", "object_name", "screenshot", "objects", "transaction"})


def _format_feature_warnings(warnings: list[Any]) -> str:
    """Render the addon's warnings as extra summary lines.

    Same "WARNING - ..." shape the connectivity audit uses, so a warning reads
    as a warning whatever produced it.
    """
    return "".join(f"\nWARNING - {w}" for w in warnings)


def _success_response(summary: str, res: dict[str, Any]) -> ToolResponse:
    """Render a successful mutation's reply WITHOUT dropping the addon's findings.

    Only ``batch`` used to merge the RPC result, so a single feature op threw
    away everything the addon had just measured: a ``pocket`` reported "created
    successfully" while its cut removed no material, and ``sketch`` never
    reported the ``dof``/``fully_constrained`` that operation_help promises
    (``describe_feature`` computes both, ``sketcher_ops`` fills them in).
    Warnings go into the summary text so a silent no-op cannot read as plain
    success; the remaining fields (dof, fully_constrained, volume_mm3, solids)
    ride along as a JSON payload. An op with nothing to report keeps returning
    its plain summary line.
    """
    extras = {k: v for k, v in res.items() if k not in _RESULT_BOOKKEEPING}
    text = summary + _format_feature_warnings(extras.pop("warnings", None) or [])
    return json_response({"summary": text, **extras}) if extras else text_response(text)


def _format_connectivity_warning(audit: dict[str, Any]) -> str:
    """Format the islands section of a verify_assembly audit as a warning.

    Pure function — returns "" when there is nothing to report or the addon
    did not return an islands section (old addon).
    """
    islands = audit.get("islands")
    if not islands:
        return ""
    count = int(audit.get("summary", {}).get("island_count", len(islands)))
    lines = [
        f"WARNING - Connectivity: {count} disconnected island(s) not touching the main assembly:"
    ]
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
    _set_last_doc(doc_name)
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
            if description:
                obj_data["description"] = description
            res = freecad.create_object(doc_name, obj_data)
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
            edit_data: dict[str, Any] = {"Properties": obj_properties or {}}
            if description:
                edit_data["description"] = description
            res = freecad.edit_object(doc_name, obj_name, edit_data)
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
            res = freecad.delete_object(doc_name, obj_name)
            success = bool(res.get("success"))
            summary = (
                f"Object '{res['object_name']}' deleted successfully"
                if success
                else f"Failed to delete object: {res.get('error')}"
            )
            params_summary = f"'{obj_name}'"
            default_desc = f"delete '{obj_name}'"
        elif operation in CAD_FEATURE_OPERATIONS:
            # Every feature op names its base, except the ones whose obj_name is
            # the object they CREATE and `color`, whose targets may instead come
            # from obj_properties.objects (obj_name="*" covers the whole document).
            needs_obj_name = operation not in CAD_NO_BASE_OPERATIONS and not (
                operation == "color" and (obj_properties or {}).get("objects")
            )
            if needs_obj_name and not obj_name:
                if operation == "color":
                    return text_response(
                        "color requires obj_name (the object to color, or '*' for every object "
                        "in the document) or obj_properties.objects"
                    )
                return text_response(f"{operation} requires obj_name (the base object)")
            params = dict(obj_properties or {})
            reserved = {"type", "base"} & set(params)
            if reserved:
                return text_response(
                    f"{operation}: obj_properties key(s) {sorted(reserved)} are reserved for the "
                    "internal feature spec and cannot be set directly. Use operation-specific keys "
                    "(see operation_help) — e.g. pocket takes 'length', not FreeCAD's 'Type' enum."
                )
            # Internal keys LAST: user params must never clobber them.
            spec = {**params, "type": operation, "base": obj_name}
            # The caller's one-line intent rides along to the addon's step
            # journal: it becomes the Steps panel's row label, so the human
            # watching the panel reads the design decision, not the call.
            if description:
                spec["description"] = description
            res = freecad.create_feature(doc_name, spec)
            success = bool(res.get("success"))
            if operation == "color":
                # Not a feature: nothing was created, so say what was painted.
                # A multi-object or redirected (feature -> Body) call would
                # otherwise read as a plain single-object success. The addon
                # caps the per-object list, so the COUNT is the honest number.
                colored = res.get("colored") or []
                count = int(res.get("colored_count", len(colored)) or 0)
                if count > 1:
                    names = ", ".join(c.get("object", "?") for c in colored[:4])
                    where = f"{count} objects ({names}, …)"
                else:
                    where = f"'{res.get('object_name')}'"
                summary = (
                    f"Appearance applied to {where}"
                    if success
                    else f"Failed to set appearance: {res.get('error')}"
                )
            else:
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
            res = freecad.execute_operations(doc_name, ops, stop_on_error)
            results = res.get("results", [])
            batch_succeeded = sum(1 for r in results if r.get("success"))
            success = bool(res.get("success"))
            summary = (
                f"Batch finished: {batch_succeeded}/{len(results)} operations succeeded"
                + (" (stopped early)" if stop_on_error and batch_succeeded < len(results) else "")
                + _BATCH_COMMIT_NOTE
            )
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
        response = _success_response(summary + step_note, res)
    else:
        return text_response(summary)
    return response


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
    # The starting object set is what session_rollback(to_step=0) must restore.
    # Without it that rollback had nothing to verify against and reported
    # success even when objects were left behind.
    initial: list[str] | None = None
    try:
        objs = freecad.get_objects(doc_name)
        # The addon answers {"success":..., "objects":[...]}; older/other paths
        # answer a bare list. Accept both.
        if isinstance(objs, dict):
            names = objs.get("objects") if objs.get("success", True) else None
        else:
            names = objs
        if names is not None:
            initial = _normalize_object_names(names)
    except Exception as e:
        logger.warning(f"session_start: could not read the document's objects: {e!s}")
    sess = new_session(name or f"Modeling {doc_name}", doc_name, initial_objects=initial)
    set_current_session(sess)
    _set_last_doc(doc_name)
    save_session(sess)
    return json_response(
        {
            "success": True,
            "session_id": sess.session_id,
            "name": sess.name,
            "doc_name": sess.doc_name,
            "message": f"Session started. Mutations via cad() on '{doc_name}' are now "
            "recorded as steps; use session(action='rollback') to backtrack. "
            "Note: FreeCAD keeps the last 20 undo steps by default (Preferences > "
            "General > Document), so a session longer than that cannot roll back past "
            "the window; session(action='status') reports when a rollback came up short.",
        }
    )


def _require_session() -> tuple[Any | None, ToolResponse | None]:
    sess = get_current_session()
    if sess is None:
        return None, text_response(
            "No active session. Use session(action='start') or session(action='resume') first."
        )
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

    # The addon keeps its own step journal for the GUI panel, and the panel can
    # roll back and re-run steps without the MCP server — so the two logs drift
    # apart. Report that instead of silently trusting this one.
    journal = _journal_snapshot(freecad, sess.doc_name) if doc_open else None
    journal_risks: list[str] = []
    # Only the journal-vs-undo-stack drift check is actionable: the journal
    # is document-lifetime (it counts pre-session work and read-only
    # execute_code inspections) while the session log is session-lifetime,
    # so the two step counts legitimately differ on nearly every document —
    # comparing them is a permanent false positive.
    if journal and journal.get("drift"):
        journal_risks.append(
            "The addon step journal no longer matches FreeCAD's undo stack "
            "(the model was undone or redone outside cad() — e.g. from the "
            "CADPilot Steps panel or Ctrl+Z). Prefer step_control for rollback."
        )

    lines = [
        f"**{sess.name}** (doc `{sess.doc_name}`, {sess.step_count} steps, {sess.status})",
    ]
    if journal:
        lines.append(
            f"Addon journal: {journal.get('done', 0)} done, "
            f"{journal.get('planned', 0)} planned"
            + (" - drift from the undo stack (WARNING)" if journal.get("drift") else "")
        )
    if suggestions:
        lines.append("\n**Next steps:**")
        lines.extend(f"- `{s['tool']}` {s['operation']}: {s['reason']}" for s in suggestions)
    if risks or journal_risks:
        lines.append("\n**Risks:**")
        lines.extend(f"- {r['message']}" for r in risks)
        lines.extend(f"- {msg}" for msg in journal_risks)
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
            "risks": risks + [{"message": msg, "severity": "warning"} for msg in journal_risks],
            "journal": journal,
            "journal_risks": journal_risks,
            "display_text": "\n".join(lines),
        }
    )


def session_get_steps_operation(limit: int = 50, offset: int = 0) -> ToolResponse:
    sess, err = _require_session()
    if err:
        return err
    steps = [s.to_dict() for s in sess.steps]
    return json_response(
        {
            "success": True,
            "session_id": sess.session_id,
            "notes": sess.notes,
            "redo_buffer": [s.to_dict() for s in sess.redo_buffer],
            "step_count": sess.step_count,
            **_page_envelope(steps, limit, offset, key="steps"),
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
    # Read the addon journal BEFORE undoing: the panel can roll back / re-run
    # behind the session log's back, and an undo rewinds the journal too — so a
    # post-undo read would hide exactly the drift this check exists to catch.
    journal = _journal_snapshot(freecad, sess.doc_name)
    try:
        res = freecad.undo_transactions(sess.doc_name, n)
    except Exception as e:
        return text_response(f"Rollback failed: {e!s}")
    if not res.get("success"):
        return text_response(f"Rollback failed in FreeCAD: {res.get('error')}")

    undone = res.get("count", n)
    ghosts = int(res.get("ghosts_skipped", 0) or 0)
    warnings = []
    if ghosts:
        # Other documents' transactions ride on this document's undo stack
        # (FreeCAD attributes an entry to the document that is ACTIVE at commit
        # time). The addon pops them out of the way without counting them; a
        # pre-fix addon counted them and reported steps it never undid.
        warnings.append(
            f"{ghosts} transaction(s) belonging to other documents were skipped while "
            "undoing (FreeCAD shares one undo stack across documents)."
        )
    if undone < n:
        warnings.append(
            f"Only {undone}/{n} transactions could be undone, so the model was NOT fully "
            "restored: steps recorded with no transaction behind them (a FreeCAD property "
            "change on its own creates no undo entry, and execute_code steps older than "
            "v0.5.2 owned none) leave their objects behind. The session log was truncated to "
            "match what actually happened. The FreeCAD-side step journal can do better: "
            "step_control rollback_to rebuilds the model from the journal, which removes "
            "those objects too."
        )
    if journal and journal.get("drift"):
        warnings.append(
            "The addon step journal reported drift from FreeCAD's undo stack before this "
            "rollback; the undo count was taken from the session log and may have rolled "
            "back more or less than intended."
        )
    removed = sess.truncate_to(sess.step_count - undone)
    save_session(sess)

    # Verify what the undo actually did instead of trusting the count. The
    # target state is the fingerprint of the step we rolled back TO, or the
    # session's starting object set at to_step=0 — which is exactly the case
    # that used to be skipped (`if sess.steps:`), so a rollback that left
    # objects behind reported success with an empty warning list.
    state_matches = None
    expected = None
    if to_step > 0:
        expected = list(sess.steps[to_step - 1].objects_after)
    elif sess.initial_objects is not None:
        expected = list(sess.initial_objects)
    if expected is not None:
        current_names = _normalize_object_names(res.get("objects", []))
        state_matches = current_names == expected
        if not state_matches:
            created = sorted(set(current_names) - set(expected))
            warnings.append(
                "Post-rollback object list differs from the recorded step fingerprint — the "
                "document was likely edited outside cad(), or undo did not reach the target "
                f"state. Still present but not expected: {created}."
            )
    elif to_step == 0:
        warnings.append(
            "The session has no recorded starting object set (it predates that field, or the "
            "addon could not be queried), so the rollback to step 0 could not be verified."
        )
    # "Success" must mean the model reached the target state, not merely that
    # FreeCAD's undo stack moved: a count-based success is exactly how a
    # rollback that popped another document's ghost entry (or a manual GUI
    # edit) reported clean while every step stayed applied.
    ok = undone >= n and state_matches is not False
    head = (
        f"Rolled back {undone} step(s) to step {sess.step_count}. "
        f"Removed: {[s.step_number for s in removed]}."
        if ok
        else (
            f"ROLLBACK INCOMPLETE: only {undone}/{n} transaction(s) were undone and the "
            f"object set does not match step {to_step}. Inspect the document before "
            "continuing; step_control rollback_to can rebuild the model from the journal."
        )
    )
    return json_response(
        {
            "success": ok,
            "rolled_back_to": sess.step_count,
            "undone_transactions": undone,
            "removed_steps": [s.step_number for s in removed],
            "state_matches_log": state_matches,
            "objects": res.get("objects", []),
            "warnings": warnings,
            "display_text": head + (" WARNING: " + " ".join(warnings) if warnings else ""),
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
    count = int(res.get("count", 0) or 0)
    if count <= 0:
        # FreeCAD's redo stack held nothing of this document. Reporting
        # success with restored_steps=[] (the old behavior) turned a diverged
        # stack into a silent no-op loop that never drained the redo buffer.
        return text_response(
            "Redo restored nothing: FreeCAD's redo stack holds no transaction for this "
            f"document, so the session's redo buffer ({len(sess.redo_buffer)} step(s)) and "
            "the document are out of sync (the stack was consumed or cleared by other "
            "work). Nothing was changed; inspect with session(action='status') before "
            "continuing."
        )
    restored = sess.restore_steps(count)
    save_session(sess)
    if len(restored) != count:
        return json_response(
            {
                "success": False,
                "restored_steps": [s.step_number for s in restored],
                "step_count": sess.step_count,
                "redo_remaining": len(sess.redo_buffer),
                "error": (
                    f"FreeCAD redid {count} transaction(s) but only {len(restored)} step(s) "
                    "came back from the redo buffer — the log is out of sync with the model."
                ),
            }
        )
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


# --- step journal (staged "plan first, execute later" modeling) --------------

_STEP_ACTIONS = (
    "run_next",
    "run_all",
    "run_to",
    "rollback_to",
    "reexecute",
    "accept",
    "reject",
    "update",
    "insert",
    "replay",
    "snapshot",
    "clear_plan",
    "reset",
    "status",
)


def _journal_snapshot(freecad: FreeCADConnection, doc_name: str) -> dict[str, Any] | None:
    """Best-effort step-journal read: None on an old addon or any failure.

    Never let a diagnostics read break the caller — the journal is an addon
    feature that a stale Mod/ install simply does not have.
    """
    try:
        res = freecad.get_step_journal(doc_name)
    except Exception:
        return None
    return res if isinstance(res, dict) and res.get("success") else None


def step_plan_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    steps: list[dict[str, Any]],
    description: str = "",
) -> ToolResponse:
    """Submit a plan to the addon's step journal WITHOUT executing it."""
    _set_last_doc(doc_name)
    if not isinstance(steps, list) or not steps:
        return text_response("step_plan requires a non-empty steps list")
    for i, step in enumerate(steps, start=1):
        if not isinstance(step, dict) or not step.get("operation"):
            return text_response(
                f"step {i} must be a dict with an 'operation' key "
                "(the same shape cad() takes: operation / obj_name / obj_type / obj_properties)"
            )
    try:
        res = freecad.journal_op(
            doc_name, {"operation": "set_plan", "steps": steps, "description": description}
        )
    except Exception as e:
        return text_response(f"step_plan failed: {e!s}")
    if not res.get("success"):
        return text_response(f"step_plan failed: {res.get('error')}")
    lines = [
        f"Plan accepted: {res.get('planned', len(steps))} step(s) queued, nothing applied yet."
    ]
    if description:
        lines.append(description)
    for i, step in enumerate(steps, start=1):
        lines.append(f"  {i}. {step.get('description') or step.get('operation')}")
    lines.append(
        "Release them in FreeCAD's CADPilot Steps panel, or call "
        "step_control(action='run_next') / action='run_all'. Then review each "
        "step: accept / reject / update / reexecute / replay — every mutating "
        "reply carries a compact journal snapshot, no extra status call needed."
    )
    return text_response("\n".join(lines))


def step_control_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    action: str,
    index: int = 0,
    params: dict[str, Any] | None = None,
    force: bool = False,
    confirm: bool = False,
) -> ToolResponse:
    """Drive the step journal: run, roll back, or re-run recorded steps."""
    _set_last_doc(doc_name)
    if action not in _STEP_ACTIONS:
        return text_response(f"unknown action '{action}'. Supported: {', '.join(_STEP_ACTIONS)}")
    spec: dict[str, Any] = {
        "operation": action,
        "index": index,
        "params": params or {},
        "force": force,
        "confirm": confirm,
    }
    if action == "insert":
        # Accept the natural forms in params: a single step dict (like update
        # takes), a "steps" list, or both forms nested. The addon only reads
        # a steps list, and an empty one fails with a misleading error.
        p = params or {}
        if "steps" in p:
            steps = p["steps"]
        elif p:
            steps = [p]
        else:
            steps = []
        if not steps or not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
            return text_response(
                "insert requires steps as a list of step dicts — pass params as a single "
                "step dict, or params={'steps': […]} for several"
            )
        spec["steps"] = steps
    try:
        res = freecad.journal_op(doc_name, spec)
    except Exception as e:
        return text_response(f"step_control failed: {e!s}")
    if not res.get("success"):
        # A bare error string hides the state the caller most needs: after a
        # replay that rolled back to 0 first, the document may be nearly empty
        # and nothing in "step 1 (batch): ValueError: …" said so (live-caught).
        detail = res.get("error") or "unknown error"
        if res.get("warning"):
            detail = f"{detail}\n{res['warning']}"
        objs = res.get("document_objects")
        if objs is not None:
            detail = f"{detail}\nThe document now holds: {', '.join(objs) or '(nothing)'}"
        return text_response(f"step_control '{action}' failed: {detail}")
    return json_response(res)


def get_addon_log_operation(
    freecad: FreeCADConnection,
    level: str | None = None,
    grep: str | None = None,
    since_seq: int = 0,
    limit: int = 100,
) -> ToolResponse:
    """Read the FreeCAD addon's debug log (ring buffer, newest last)."""
    try:
        res = freecad.get_addon_log(level, grep, since_seq, limit)
    except AttributeError:
        # A stale Mod/ install simply has no such RPC. Say so usefully instead
        # of leaking an attribute error about the client proxy.
        return text_response(
            "The FreeCAD addon does not provide a debug log (it predates this "
            "feature). Upgrade the addon in FreeCAD's Mod/ directory."
        )
    except Exception as e:
        return text_response(f"Could not read the addon log: {e!s}")
    if not isinstance(res, dict) or not res.get("success"):
        error = res.get("error") if isinstance(res, dict) else res
        return text_response(f"Could not read the addon log: {error}")
    if not res.get("records"):
        return text_response(f"No records match. Log status: {res.get('status')}")
    return json_response(res)


def diagnose_operation(
    host: str,
    port: int = 9875,
    timeout: float = 5.0,
) -> ToolResponse:
    """Fault diagnosis that runs entirely on the MCP side (FreeCAD may be down)."""
    from ..diagnostics import diagnose, format_report

    return text_response(format_report(diagnose(host, port, timeout)))


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
            "message": f"Session '{sess.name}' paused and saved. Resume with session(action='resume', session_id='{sess.session_id}').",
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
    _set_last_doc(sess.doc_name)
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


_SESSION_ACTIONS = (
    "start",
    "status",
    "get_steps",
    "rollback",
    "redo",
    "add_note",
    "pause",
    "resume",
    "list",
    "complete",
)


def session_action_operation(
    freecad: FreeCADConnection,
    action: str,
    *,
    doc_name: str | None = None,
    session_id: str = "",
    name: str = "",
    create_document: bool = False,
    to_step: int | None = None,
    n: int = 1,
    force: bool = False,
    note: str = "",
    note_type: str = "observation",
    save: bool = False,
    save_path: str | None = None,
    description: str = "",
    tags: list[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ToolResponse:
    """Dispatch the unified ``session`` tool to the per-action operation."""
    if action == "start":
        if not doc_name:
            return text_response("session(action='start') requires doc_name")
        return session_start_operation(freecad, doc_name, name, create_document)
    # The current session is ONE global slot per MCP server process, and several
    # agents share that process: a status/rollback that trusts the slot answers
    # for — or undoes — the OTHER agent's document (live: an agent's rollback
    # was refused with the stranger session's step range while its own document
    # had 6 valid undo steps). Every call carries doc_name, so a mismatch is
    # detectable; say it instead of acting.
    guarded = ("status", "get_steps", "rollback", "redo", "add_note", "pause", "complete")
    if action in guarded and (err := _session_doc_mismatch(doc_name)):
        return text_response(err)
    if action == "status":
        return session_status_operation(freecad)
    if action == "get_steps":
        return session_get_steps_operation(limit, offset)
    if action == "rollback":
        if to_step is None:
            # No safe default here: 0 undoes ALL steps, so omitting to_step
            # must fail loudly instead of wiping the whole session.
            return text_response(
                "session(action='rollback') requires to_step (keep steps 1..to_step; 0 = undo all)"
            )
        return session_rollback_operation(freecad, to_step, force)
    if action == "redo":
        return session_redo_operation(freecad, n)
    if action == "add_note":
        return session_add_note_operation(note, note_type)
    if action == "pause":
        return session_pause_operation()
    if action == "resume":
        if not session_id:
            return text_response(
                "session(action='resume') requires session_id (see session(action='list'))"
            )
        return session_resume_operation(freecad, session_id)
    if action == "list":
        return session_list_operation()
    if action == "complete":
        return session_complete_operation(freecad, save, save_path, description, tags)
    return text_response(
        f"unknown session action '{action}'. Supported: {', '.join(_SESSION_ACTIONS)}"
    )


# Said on every batch summary: a partially failed batch COMMITS the ops that
# worked (one undo step removes the whole batch), which a bare "3/4 succeeded"
# left ambiguous — callers read it as "the batch was rolled back".
_BATCH_COMMIT_NOTE = (
    " — the successful ops ARE committed (the whole batch is one undo step; "
    "step_control rollback_to removes it)"
)


def _session_doc_mismatch(doc_name: str | None) -> str:
    """Refuse a session action aimed at a document the active session is not on.

    Only checked when the caller NAMED a document (doc_name=None keeps the
    single-agent flow working) — a client that says which document it means
    must never get away with acting on another one.
    """
    current = get_current_session()
    if current is None or not doc_name or not current.doc_name:
        return ""
    if doc_name != current.doc_name:
        return (
            f"the active session {current.session_id} tracks document "
            f"'{current.doc_name}', not '{doc_name}' (another agent or a restarted "
            "client owns it). Pass its document's name, or start/resume a session "
            f"for '{doc_name}'."
        )
    return ""


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
    _set_last_doc(doc_name)
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
    doc_name: str,
    obj_name: str,
    anchors: dict[str, Any],
    replace: bool = False,
    coord_frame: str = "local",
) -> ToolResponse:
    if not anchors:
        return text_response("set_anchors requires a non-empty anchors dict")
    _set_last_doc(doc_name)
    try:
        res = freecad.set_anchors(
            doc_name,
            obj_name,
            anchors,
            replace,
            coord_frame,
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
    return response


def assemble_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    mates: list[dict[str, Any]],
    tolerance: float = 0.1,
    stop_on_error: bool = True,
) -> ToolResponse:
    if not mates:
        return text_response("assemble requires a non-empty mates list")
    _set_last_doc(doc_name)
    try:
        res = freecad.assemble(
            doc_name,
            mates,
            tolerance,
            stop_on_error,
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
    return response


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
