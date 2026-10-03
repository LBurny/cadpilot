import logging
import os
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

try:
    # mcp 1.x
    from mcp.server.fastmcp import Context, FastMCP
except ImportError:
    # mcp 2.x moved mcp.server.fastmcp to mcp.server.mcpserver and renamed
    # FastMCP to MCPServer; the API surface used here is unchanged.
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver import MCPServer as FastMCP
from mcp.types import ImageContent, TextContent

from .freecad_client import FreeCADConnection
from .operations import (
    align_shapes_operation,
    assemble_operation,
    assembly_session_operation,
    cad_operation,
    check_interference_operation,
    create_document_operation,
    diagnose_operation,
    execute_code_async_operation,
    execute_code_operation,
    get_addon_log_operation,
    get_anchors_operation,
    get_object_operation,
    get_objects_operation,
    get_positioning_info_operation,
    get_task_result_operation,
    get_topology_operation,
    get_view_operation,
    inspect_freecad_operation,
    list_documents_operation,
    measure_geometry_operation,
    operation_help_operation,
    recall_patterns_operation,
    save_pattern_operation,
    session_add_note_operation,
    session_complete_operation,
    session_get_steps_operation,
    session_list_operation,
    session_pause_operation,
    session_redo_operation,
    session_resume_operation,
    session_rollback_operation,
    session_start_operation,
    session_status_operation,
    set_anchors_operation,
    step_control_operation,
    step_plan_operation,
    verify_assembly_operation,
)
from .prompt_text import ASSET_CREATION_STRATEGY
from .server_state import ServerState

logging.basicConfig(
    level=logging.WARNING, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)


def _resolve_mcp_log_level(raw: str | None) -> str:
    """A valid level name from ``CADPILOT_LOG_LEVEL``; INFO when unusable.

    ``logger.setLevel`` raises ValueError on an unknown name, so this has to be
    validated rather than passed straight through — a typo in the environment
    must not stop the server from starting.
    """
    name = (raw or "INFO").upper()
    return name if isinstance(logging.getLevelName(name), int) else "INFO"


logger = logging.getLogger("CADPilot")
# Previously hardcoded to INFO; CADPILOT_LOG_LEVEL makes a debug session
# possible without editing code. basicConfig only sets the ROOT level, so this
# explicit level is what actually gates these records.
logger.setLevel(_resolve_mcp_log_level(os.environ.get("CADPILOT_LOG_LEVEL")))

# Records mirrored out of the addon (see _maybe_start_log_forwarder) get their
# own logger so they stay distinguishable from this process's own output.
addon_logger = logging.getLogger("CADPilot.addon")

state = ServerState()


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    try:
        logger.info("CADPilot server starting up")
        try:
            _ = get_freecad_connection()
            logger.info("Successfully connected to FreeCAD on startup")
        except Exception as e:
            logger.warning(f"Could not connect to FreeCAD on startup: {e!s}")
            logger.warning(
                "Make sure the FreeCAD addon is running before using FreeCAD resources or tools"
            )
        yield {}
    finally:
        if state.freecad_connection:
            logger.info("Disconnecting from FreeCAD on shutdown")
            state.freecad_connection.disconnect()
            state.freecad_connection = None
        logger.info("CADPilot server shut down")


mcp = FastMCP(
    "CADPilot",
    instructions="FreeCAD integration through the Model Context Protocol",
    lifespan=server_lifespan,
)


def get_freecad_connection() -> FreeCADConnection:
    """Get or create a persistent FreeCAD connection"""
    if state.freecad_connection is None:
        state.freecad_connection = FreeCADConnection(host=state.rpc_host, port=9875)
        if not state.freecad_connection.ping():
            logger.error("Failed to ping FreeCAD")
            state.freecad_connection = None
            raise Exception("Failed to connect to FreeCAD. Make sure the FreeCAD addon is running.")
    return state.freecad_connection


# Opt-in mirror of the addon's log into this process's stderr. Off by default:
# it costs a poll every 2s and duplicates records a caller may also fetch via
# get_addon_log. Turn it on (CADPILOT_FORWARD_ADDON_LOG=1) when driving FreeCAD
# unattended — that way a wedged addon still leaves its last activity visible in
# the MCP process's own output, where get_addon_log can no longer reach it.
_FORWARD_INTERVAL = 2.0
_log_forwarder: threading.Thread | None = None


def _maybe_start_log_forwarder() -> None:
    """Start the poller when CADPILOT_FORWARD_ADDON_LOG is set. Never raises."""
    global _log_forwarder
    if _log_forwarder is not None:
        return
    if os.environ.get("CADPILOT_FORWARD_ADDON_LOG", "").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        return

    def loop() -> None:
        cursor = 0
        while True:
            time.sleep(_FORWARD_INTERVAL)
            try:
                res = get_freecad_connection().get_addon_log(
                    level="INFO", grep=None, since_seq=cursor, limit=50
                )
            except Exception:
                continue  # addon busy or restarting; the next tick retries
            if not isinstance(res, dict) or not res.get("success"):
                continue
            for record in res.get("records") or []:
                cursor = max(cursor, int(record.get("seq", cursor)))
                addon_logger.info(
                    "[addon %s %s %s] %s",
                    record.get("level"),
                    record.get("request"),
                    record.get("name"),
                    record.get("message"),
                )

    _log_forwarder = threading.Thread(target=loop, name="cadpilot-log-forward", daemon=True)
    _log_forwarder.start()
    logger.info("Forwarding the addon log to this process's stderr every %ss", _FORWARD_INTERVAL)


@mcp.tool()
def create_document(
    ctx: Context, name: str, with_screenshot: bool | None = None
) -> list[TextContent | ImageContent]:
    """Create a new document in FreeCAD.

    Args:
        name: Document name.
    """
    return create_document_operation(
        get_freecad_connection(), name, with_screenshot=state.resolve_screenshot(with_screenshot)
    )


@mcp.tool()
def cad(
    ctx: Context,
    operation: Literal[
        "create_object",
        "edit_object",
        "delete_object",
        "batch",
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
    ],
    doc_name: str,
    obj_type: str | None = None,
    obj_name: str | None = None,
    obj_properties: dict[str, Any] | None = None,
    ops: list[dict[str, Any]] | None = None,
    stop_on_error: bool = False,
    description: str = "",
    with_screenshot: bool | None = None,
) -> list[TextContent | ImageContent]:
    """CAD modeling operation (unified mutation tool).

    Feature ops take obj_name as the BASE object (for sketch/variables/
    datum_plane/hull it names the NEW object) and params via obj_properties.
    With an active session, mutations are recorded as rollback-able steps.
    Reference: operation_help("<operation>").

    Args:
        obj_type: Object type for create_object (e.g. "Part::Box").
        obj_name: Base object (new-object name for sketch/variables/
            datum_plane/hull).
        obj_properties: Properties/params for the operation.
        ops: Operation dicts for batch.
        stop_on_error: batch — stop at the first failed op.
        description: Note recorded into the session step log.
    """
    return cad_operation(
        get_freecad_connection(),
        state.resolve_screenshot(with_screenshot),
        operation,
        doc_name,
        obj_type=obj_type,
        obj_name=obj_name,
        obj_properties=obj_properties,
        ops=ops,
        stop_on_error=stop_on_error,
        description=description,
        auto_audit=state.auto_audit,
    )


@mcp.tool()
def execute_code_async(ctx: Context, code: str) -> list[TextContent]:
    """Execute Python code in FreeCAD without waiting (background thread, NOT
    the GUI thread): the code must not touch FreeCADGui, view/selection,
    document objects, recompute, or save — use execute_code for any of that.
    Only for long pure CPU computations on already-fetched shapes. Use
    task_print(...) for output (print() is not captured); poll with
    get_task_result.

    Args:
        code: Background-safe Python code.
    """
    return execute_code_async_operation(get_freecad_connection(), code)


@mcp.tool()
def get_task_result(ctx: Context, task_id: str) -> list[TextContent]:
    """Get the status and captured output of an execute_code_async task.

    Args:
        task_id: Task ID from execute_code_async.

    Returns:
        JSON {status: running | done | error, output, traceback on error}.
    """
    return get_task_result_operation(get_freecad_connection(), task_id)


@mcp.tool()
def execute_code(
    ctx: Context,
    code: str,
    with_screenshot: bool | None = None,
) -> list[TextContent | ImageContent]:
    """Execute arbitrary Python in FreeCAD's GUI thread (full FreeCAD API;
    FreeCAD/FreeCADGui already imported). print() output is returned.

    Args:
        code: Python code to execute.
    """
    return execute_code_operation(
        get_freecad_connection(), state.resolve_screenshot(with_screenshot), code
    )


@mcp.tool()
def get_view(
    ctx: Context,
    view_name: Literal[
        "Isometric", "Front", "Top", "Right", "Back", "Left", "Bottom", "Dimetric", "Trimetric"
    ],
    width: int | None = None,
    height: int | None = None,
    focus_object: str | None = None,
) -> list[ImageContent | TextContent]:
    """Get a screenshot of the active view.

    Args:
        view_name: Camera view.
        width/height: Pixels; default is the viewport size.
        focus_object: Object to focus on; default fits all objects.
    """
    return get_view_operation(get_freecad_connection(), view_name, width, height, focus_object)


@mcp.tool()
def get_objects(
    ctx: Context,
    doc_name: str,
    with_screenshot: bool | None = None,
) -> list[TextContent | ImageContent]:
    """Get all objects in a document (screenshot only when requested)."""
    return get_objects_operation(
        get_freecad_connection(), state.resolve_screenshot(with_screenshot), doc_name
    )


@mcp.tool()
def get_object(
    ctx: Context,
    doc_name: str,
    obj_name: str,
    with_screenshot: bool | None = None,
) -> list[TextContent | ImageContent]:
    """Get an object's properties from a document (screenshot only when
    requested)."""
    return get_object_operation(
        get_freecad_connection(),
        state.resolve_screenshot(with_screenshot),
        doc_name,
        obj_name,
    )


@mcp.tool()
def list_documents(ctx: Context) -> list[TextContent]:
    """Get the names of open documents in FreeCAD."""
    return list_documents_operation(get_freecad_connection())


# ---------------------------------------------------------------------------
# Modeling sessions (step recording + rollback), pattern memory, introspection
# ---------------------------------------------------------------------------


@mcp.tool()
def session_start(
    ctx: Context,
    doc_name: str,
    name: str = "",
    create_document: bool = False,
) -> list[TextContent]:
    """Start a modeling session bound to a document: every successful cad()
    mutation is recorded as a transaction-backed step, so session_rollback
    enables trial-and-error modeling. execute_code steps are non-atomic
    (rolling back past them needs force).

    Args:
        name: Optional human-readable session name.
        create_document: Create the document first if it does not exist.

    Returns: Session info including session_id.
    """
    return session_start_operation(get_freecad_connection(), doc_name, name, create_document)


@mcp.tool()
def session_status(ctx: Context) -> list[TextContent]:
    """Show the active session: step count, document state, next-step
    suggestions, and risks (e.g. drift from GUI edits, non-atomic steps).

    Returns:
        JSON summary with a human-readable display_text.
    """
    return session_status_operation(get_freecad_connection())


@mcp.tool()
def session_get_steps(ctx: Context) -> list[TextContent]:
    """Return all recorded steps and notes of the active session.

    Returns:
        JSON list (step_number, operation, description, params, objects_after
        fingerprint, atomic flag, timestamp).
    """
    return session_get_steps_operation()


@mcp.tool()
def session_rollback(
    ctx: Context,
    to_step: int,
    force: bool = False,
) -> list[TextContent]:
    """Roll back the model to a previous step: undoes the matching document
    transactions and truncates the step log; removed steps sit in a redo
    buffer until a new cad() call discards them.

    Args:
        to_step: Keep steps 1..to_step; undo everything after (0 = undo all).
        force: Roll back even across non-atomic execute_code steps (risky:
            undo may revert the wrong change).
    """
    return session_rollback_operation(get_freecad_connection(), to_step, force)


@mcp.tool()
def session_redo(ctx: Context, n: int = 1) -> list[TextContent]:
    """Redo n previously rolled-back steps (valid until a new cad() call).

    Args:
        n: Number of steps to restore (default 1).
    """
    return session_redo_operation(get_freecad_connection(), n)


@mcp.tool()
def session_add_note(
    ctx: Context,
    note: str,
    note_type: str = "observation",
) -> list[TextContent]:
    """Attach a human/LLM insight to the session log (not an operation step).

    Args:
        note: Note content.
        note_type: "observation" | "assumption" | "limitation" | "correction".
    """
    return session_add_note_operation(note, note_type)


@mcp.tool()
def step_plan(
    ctx: Context,
    doc_name: str,
    steps: list[dict[str, Any]],
    description: str = "",
) -> list[TextContent]:
    """Submit a modeling plan to FreeCAD without executing it.

    Steps are cad() argument dicts that wait for release in the CADPilot Steps
    panel (or via step_control). Reference: operation_help("step_plan").
    """
    return step_plan_operation(get_freecad_connection(), doc_name, steps, description)


@mcp.tool()
def step_control(
    ctx: Context,
    doc_name: str,
    action: str,
    index: int = 0,
    params: dict[str, Any] | None = None,
    force: bool = False,
    confirm: bool = False,
) -> list[TextContent]:
    """Run, review, and edit steps in a document's step journal.

    Actions: run_next | run_all | run_to | rollback_to | reexecute |
    accept | reject | update | insert | replay | snapshot | clear_plan |
    reset (needs confirm=true) | status. Reference: operation_help("step_control").
    """
    return step_control_operation(
        get_freecad_connection(), doc_name, action, index, params, force, confirm
    )


@mcp.tool()
def get_addon_log(
    ctx: Context,
    level: str = "INFO",
    grep: str = "",
    since_seq: int = 0,
    limit: int = 100,
) -> list[TextContent]:
    """Read the FreeCAD addon's debug log (newest last). Use it when a call
    misbehaves or hangs: RPC timings, GUI dispatch, transactions, journal ops.

    Args:
        level: Min level: DEBUG | INFO | WARNING | ERROR.
        grep: Substring filter on message and detail.
        since_seq: Only records after this sequence number.
        limit: Max records (default 100).
    """
    return get_addon_log_operation(
        get_freecad_connection(), level or None, grep or None, since_seq, limit
    )


@mcp.tool()
def diagnose(ctx: Context, host: str | None = None) -> list[TextContent]:
    """Diagnose why CADPilot cannot reach FreeCAD — runs while FreeCAD is down
    or frozen. Probes the RPC port, the FreeCAD process, the addon install and
    its logs (incl. the bootstrap crash log) on Windows/macOS/Linux.

    Args:
        host: FreeCAD host to probe; defaults to this server's --host.
    """
    return diagnose_operation(host or state.rpc_host)


@mcp.tool()
def session_pause(ctx: Context) -> list[TextContent]:
    """Pause the active session (persisted to disk; resume later). Returns
    the session_id."""
    return session_pause_operation()


@mcp.tool()
def session_resume(ctx: Context, session_id: str) -> list[TextContent]:
    """Resume a paused/completed session from disk.

    Args:
        session_id: The session to resume (see session_list). Warns if the
            bound document is no longer open.
    """
    return session_resume_operation(get_freecad_connection(), session_id)


@mcp.tool()
def session_list(ctx: Context) -> list[TextContent]:
    """List all persisted sessions (most recently updated first).

    Returns:
        JSON list with session_id, name, doc_name, status, step_count.
    """
    return session_list_operation()


@mcp.tool()
def session_complete(
    ctx: Context,
    save: bool = False,
    save_path: str | None = None,
    description: str = "",
    tags: list[str] | None = None,
) -> list[TextContent]:
    """Complete the active session: store the workflow as a reusable pattern
    (recall later with recall_patterns) and optionally save the document.

    Args:
        save: Save the FreeCAD document (.FCStd).
        save_path: Target file path (saveAs); omit to save in place.
        description: What this workflow builds (stored with the pattern).
        tags: Retrieval tags for the pattern.

    Returns:
        JSON with pattern_id and save result.
    """
    return session_complete_operation(get_freecad_connection(), save, save_path, description, tags)


@mcp.tool()
def save_pattern(
    ctx: Context,
    name: str,
    description: str,
    code: str = "",
    tags: list[str] | None = None,
) -> list[TextContent]:
    """Store a reusable modeling pattern (code snippet or workflow) — call
    this after a non-trivial approach worked.

    Knowledge hierarchy: 1) your own knowledge, 2) recall_patterns,
    3) inspect_freecad — then store new approaches back here.

    Args:
        name: Short pattern name (e.g. "flanged pipe via loft").
        description: What it does and when to use it.
        code: Optional Python snippet that implements it.
        tags: Retrieval tags.

    Returns: The stored pattern_id.
    """
    return save_pattern_operation(name, description, code, tags)


@mcp.tool()
def recall_patterns(
    ctx: Context,
    query: str,
    limit: int = 3,
) -> list[TextContent]:
    """Search the pattern memory for workflows/code similar to your task;
    use before trial-and-error when your own knowledge is insufficient.

    Args:
        query: Keywords describing the task (e.g. "boolean cut holes cylinder").
        limit: Max results (default 3).

    Returns:
        JSON list of matching patterns with code/steps.
    """
    return recall_patterns_operation(query, limit)


@mcp.tool()
def operation_help(ctx: Context, operation: str | None = None) -> list[TextContent]:
    """Full parameter reference for a cad() operation or assembly_session.

    Detailed docs live here (not in docstrings) to keep the tool list small
    in context. Call with an operation name (e.g. "sketch", "hull",
    "assembly_session") or none for the topic list.
    """
    return operation_help_operation(operation)


@mcp.tool()
def inspect_freecad(
    ctx: Context,
    doc_name: str | None = None,
    obj_name: str | None = None,
    dotted_name: str | None = None,
) -> list[TextContent]:
    """Runtime introspection of the FreeCAD Python API — last resort when
    your knowledge and recall_patterns are insufficient.

    Modes: doc_name + obj_name gives the object's TypeId, settable
    properties (with types), public methods, docstring; dotted_name (e.g.
    "Part.makeLoft") gives its docstring or a module/class member list.

    Returns:
        JSON with properties/members/docstring (compact, capped).
    """
    return inspect_freecad_operation(get_freecad_connection(), doc_name, obj_name, dotted_name)


@mcp.tool()
def measure_geometry(ctx: Context, doc_name: str, obj_name: str) -> list[TextContent]:
    """Measure an object's Shape (must have one); use to verify design
    targets quantitatively after modeling steps.

    Returns: JSON volume_mm3, area_mm2, bbox, center_of_mass, element
    counts, is_valid, shape_type.
    """
    return measure_geometry_operation(get_freecad_connection(), doc_name, obj_name)


@mcp.tool()
def get_topology(
    ctx: Context,
    doc_name: str,
    obj_name: str,
    element: Literal["faces", "edges", "vertices"] = "faces",
    limit: int = 50,
    offset: int = 0,
) -> list[TextContent]:
    """List an object's faces/edges/vertices for selection — faces by area,
    edges by length, vertices by distance (largest first). Use the
    index/name (Face1, Edge3, ...) in fillet, boolean, sketch-on-face, etc.

    Args:
        element: "faces" | "edges" | "vertices".
        limit: Max entries (1-200, default 50).
        offset: Skip this many entries (pagination).

    Returns: total, returned, list(index, name, type, area/length, center,
    normal for planar faces).
    """
    return get_topology_operation(
        get_freecad_connection(), doc_name, obj_name, element, limit, offset
    )


@mcp.tool()
def check_interference(ctx: Context, doc_name: str, obj_a: str, obj_b: str) -> list[TextContent]:
    """Distance and intersection (common volume) between two objects; use
    to verify clearance or detect collisions.

    Returns: JSON distance_mm, intersects, common_volume_mm3.
    """
    return check_interference_operation(get_freecad_connection(), doc_name, obj_a, obj_b)


@mcp.tool()
def get_positioning_info(
    ctx: Context,
    doc_name: str,
    obj_name: str,
    element: Literal["face", "edge", "vertex"],
    element_index: int,
) -> list[TextContent]:
    """Global-coordinate spatial info for one face/edge/vertex (center,
    normal, axis, radius, endpoints — the object's Placement already
    applied). Use instead of get_topology when you need precise positioning
    for alignment or assembly.

    Args:
        element: "face" | "edge" | "vertex".
        element_index: 0-based index (use get_topology to find indices).
    """
    return get_positioning_info_operation(
        get_freecad_connection(), doc_name, obj_name, element, element_index
    )


@mcp.tool()
def align_shapes(
    ctx: Context,
    doc_name: str,
    obj_name: str,
    element: Literal["face", "edge", "vertex"],
    element_index: int,
    target_obj: str,
    target_element: Literal["face", "edge", "vertex"],
    target_element_index: int,
    mode: Literal["touch", "center", "axis"] = "touch",
    offset: float = 0.0,
) -> list[TextContent]:
    """Move an object so one of its elements aligns with a target element.

    Args:
        element / element_index: Element on the object to move.
        target_element / target_element_index: Element on the target.
        mode: "touch" (face-to-face, normals opposing) | "center" (centers
            coincide) | "axis" (cylindrical axes aligned).
        offset: Extra distance along the target normal (positive = away).

    Returns:
        JSON with success and the new Placement.
    """
    return align_shapes_operation(
        get_freecad_connection(),
        doc_name,
        obj_name,
        element,
        element_index,
        target_obj,
        target_element,
        target_element_index,
        mode,
        offset,
    )


@mcp.tool()
def get_anchors(ctx: Context, doc_name: str, obj_name: str) -> list[TextContent]:
    """List an object's assembly anchors in GLOBAL coordinates (read-only):
    auto-derived (bbox_center/min/max, com, axis_mid/start/end for the
    dominant cylindrical face, face0..2_center for the largest planar faces)
    merged with explicit set_anchors ones (explicit wins). Call this BEFORE
    placing parts and plan mates from the returned numbers — never guess
    coordinates.

    Returns:
        JSON with anchors: {name: {pos, dir, source: "auto"|"explicit"}}.
    """
    return get_anchors_operation(get_freecad_connection(), doc_name, obj_name)


@mcp.tool()
def set_anchors(
    ctx: Context,
    doc_name: str,
    obj_name: str,
    anchors: dict[str, Any],
    replace: bool = False,
    coord_frame: Literal["local", "global"] = "local",
    with_screenshot: bool | None = None,
) -> list[TextContent]:
    """Define explicit named anchors on an object. Anchors persist with the
    document and follow Placement moves. Records a modeling-session step.

    Args:
        anchors: {name: {"pos": [x, y, z], "dir": [x, y, z] | null}}.
        replace: Replace all existing anchors instead of merging.
        coord_frame: "local" (stored as-is) or "global" (converted — use
            whenever your source coordinates are global).
    """
    return set_anchors_operation(
        get_freecad_connection(),
        state.resolve_screenshot(with_screenshot),
        doc_name,
        obj_name,
        anchors,
        replace,
        coord_frame,
    )


@mcp.tool()
def assemble(
    ctx: Context,
    doc_name: str,
    mates: list[dict[str, Any]],
    tolerance: float = 0.1,
    stop_on_error: bool = True,
    with_screenshot: bool | None = None,
) -> list[TextContent]:
    """Assemble parts by snapping named anchors together (ONE transaction);
    mates over tolerance fail and roll back. For PERSISTENT joints use
    assembly_session. Reference: operation_help("assemble").

    Args:
        mates: Non-empty list of mate dicts.
        tolerance: Max allowed post-move residual in mm (default 0.1).
        stop_on_error: Abort and roll back at the first failed mate.

    Returns: JSON per-mate residuals and passed/failed counts.
    """
    return assemble_operation(
        get_freecad_connection(),
        state.resolve_screenshot(with_screenshot),
        doc_name,
        mates,
        tolerance,
        stop_on_error,
    )


@mcp.tool()
def verify_assembly(
    ctx: Context,
    doc_name: str,
    checks: list[dict[str, Any]] | None = None,
    float_threshold: float = 1.0,
    interference_min_volume: float = 1.0,
) -> list[TextContent]:
    """Audit the document's spatial sanity (read-only): floating parts,
    interferences, and distances for requested anchor pairs. Hidden objects
    are skipped. Prefer this numeric health report over eyeballing
    screenshots.

    Args:
        checks: Optional anchor-pair distance checks.
        float_threshold: Nearest-neighbour gap (mm) for "floating" (default 1.0).
        interference_min_volume: Minimum common volume (mm3) to report.

    Returns: JSON floating/interferences/checks lists and a summary.
    """
    return verify_assembly_operation(
        get_freecad_connection(),
        doc_name,
        checks,
        float_threshold,
        interference_min_volume,
    )


@mcp.tool()
def assembly_session(
    ctx: Context,
    operation: str,
    doc_name: str | None = None,
    name: str | None = None,
    part: str | None = None,
    joint: str | None = None,
    a: dict[str, Any] | None = None,
    b: dict[str, Any] | None = None,
    joint_type: str = "fixed",
    trim: dict[str, Any] | None = None,
    to_step: int | None = None,
    gap_samples: int = 8,
) -> list[TextContent]:
    """Independent assembly state machine with PERSISTENT joints (FreeCAD
    Assembly workbench) — the mate-based counterpart to one-shot `assemble`.

    Workflow: start(ground=part) -> add_component(part) -> mate(a, b,
    joint_type, trim?) -> solve -> verify -> complete. Joints persist: move
    a parent part, call solve, and children follow. rollback(to_step)
    restores placements atomically. A mate ref is {"part": <name>} plus
    exactly ONE of face="FaceN" / anchor=<name> / point=[x,y,z].
    Reference: operation_help("assembly_session").
    """
    return assembly_session_operation(
        get_freecad_connection(),
        operation,
        doc_name=doc_name,
        name=name,
        part=part,
        joint=joint,
        a=a,
        b=b,
        joint_type=joint_type,
        trim=trim,
        to_step=to_step,
        gap_samples=gap_samples,
    )


@mcp.prompt()
def asset_creation_strategy() -> str:
    return ASSET_CREATION_STRATEGY


def _validate_host(value: str) -> str:
    """Validate that *value* is a valid IP address or hostname.

    Used as the ``type`` callback for the ``--host`` argparse argument.
    Raises ``argparse.ArgumentTypeError`` on invalid input.
    """
    import argparse

    import validators

    if validators.ipv4(value) or validators.ipv6(value) or validators.hostname(value):
        return value
    raise argparse.ArgumentTypeError(
        f"Invalid host: '{value}'. Must be a valid IP address or hostname."
    )


def main():
    """Run the MCP server"""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--only-text-feedback",
        action="store_true",
        help="Never return screenshots, even when a tool call requests one (for text-only models)",
    )
    parser.add_argument(
        "--with-screenshots",
        action="store_true",
        help="Attach a screenshot to every mutation/read tool response by default (tools can still opt out per call)",
    )
    parser.add_argument(
        "--host",
        type=_validate_host,
        default="localhost",
        help="Host address of the FreeCAD RPC server to connect to (default: localhost)",
    )
    parser.add_argument(
        "--no-auto-audit",
        action="store_true",
        help="Disable the automatic connectivity audit after cad() mutations",
    )
    args = parser.parse_args()
    state.only_text_feedback = args.only_text_feedback
    state.with_screenshots = args.with_screenshots
    state.rpc_host = args.host
    state.auto_audit = not args.no_auto_audit
    if state.only_text_feedback and state.with_screenshots:
        logger.warning(
            "Both --only-text-feedback and --with-screenshots given; --only-text-feedback wins"
        )
    logger.info(f"Only text feedback: {state.only_text_feedback}")
    logger.info(f"Screenshots by default: {state.with_screenshots}")
    logger.info(f"Auto connectivity audit: {state.auto_audit}")
    logger.info(f"Connecting to FreeCAD RPC server at: {state.rpc_host}")
    _maybe_start_log_forwarder()
    mcp.run()
