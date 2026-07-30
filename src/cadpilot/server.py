import logging
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
    execute_code_async_operation,
    execute_code_operation,
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
    verify_assembly_operation,
)
from .prompt_text import ASSET_CREATION_STRATEGY
from .server_state import ServerState

logging.basicConfig(
    level=logging.WARNING, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("CADPilot")
logger.setLevel(logging.INFO)

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


@mcp.tool()
def create_document(
    ctx: Context, name: str, with_screenshot: bool | None = None
) -> list[TextContent | ImageContent]:
    """Create a new document in FreeCAD.

    Args:
        name: The name of the document to create.
        with_screenshot: Attach a screenshot of the result (default: no screenshot).

    Returns:
        A message indicating the success or failure of the document creation.

    Examples:
        If you want to create a document named "MyDocument", you can use the following data.
        ```json
        {
            "name": "MyDocument"
        }
        ```
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
    """Perform a CAD modeling operation (unified mutation tool).

    When a modeling session is active (session_start), successful operations
    on the session's document are recorded as steps and can be rolled back
    with session_rollback.

    Feature ops take obj_name as the BASE object (for sketch/variables/
    datum_plane/hull it names the NEW object) and params via obj_properties.
    Every mutation runs in one FreeCAD transaction and is auto-audited for
    connectivity. Call operation_help("<operation>") for the full parameter
    reference of any operation — e.g. operation_help("sketch").

    Args:
        operation: The operation to perform.
        doc_name: The document to operate on.
        obj_type: Object type for create_object (e.g. "Part::Box").
        obj_name: Base object name (new-object name for sketch/variables/
            datum_plane/hull).
        obj_properties: Properties/params for the operation.
        ops: Operation dicts for batch.
        stop_on_error: batch — stop at the first failed op.
        description: Note recorded into the session step log.
        with_screenshot: Attach a screenshot (default: none; use get_view).

    Returns:
        Result message (batch: JSON with per-op results), step number when a
        session is active, and a screenshot only when requested.
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
    """Execute Python code in FreeCAD without waiting for completion.

    Background thread, NOT the GUI thread: the code must NOT touch FreeCADGui,
    the active view/selection, document objects, recompute, or save — for any
    of that use execute_code instead (the safe default). Only for long pure
    OCCT/CPU computations on already-fetched shapes. Use task_print(...) for
    output (print() is not captured); poll with get_task_result(task_id).

    Args:
        code: Background-safe Python code to execute.

    Returns:
        A message with the task_id for polling via get_task_result.
    """
    return execute_code_async_operation(get_freecad_connection(), code)


@mcp.tool()
def get_task_result(ctx: Context, task_id: str) -> list[TextContent]:
    """Get the status and captured output of a background task started by execute_code_async.

    Args:
        task_id: The task ID returned by execute_code_async.

    Returns:
        A JSON object with status ("running" | "done" | "error"), output captured
        via task_print(...), and the error traceback if the task failed.
    """
    return get_task_result_operation(get_freecad_connection(), task_id)


@mcp.tool()
def execute_code(
    ctx: Context,
    code: str,
    with_screenshot: bool | None = None,
) -> list[TextContent | ImageContent]:
    """Execute arbitrary Python code in FreeCAD.

    The code runs on FreeCAD's GUI thread with the full FreeCAD Python API
    available (FreeCAD, FreeCADGui already imported; import Part/Draft/etc.
    as needed). print() output is captured and returned.

    Args:
        code: The Python code to execute.
        with_screenshot: Attach a screenshot after successful execution
            (default: no screenshot).

    Returns:
        A message indicating the success or failure of the code execution, the
        output of the code execution, and a screenshot only when requested.
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
        view_name: Camera view ("Isometric", "Front", "Top", ...).
        width/height: Pixels; defaults to the viewport size.
        focus_object: Object to focus on; default fits all objects.

    Returns:
        A screenshot of the active view.
    """
    return get_view_operation(get_freecad_connection(), view_name, width, height, focus_object)


@mcp.tool()
def get_objects(
    ctx: Context,
    doc_name: str,
    with_screenshot: bool | None = None,
) -> list[TextContent | ImageContent]:
    """Get all objects in a document.

    Args:
        doc_name: The name of the document to get the objects from.
        with_screenshot: Attach a screenshot of the document (default: no screenshot).

    Returns:
        A list of objects in the document, and a screenshot only when requested.
    """
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
    """Get an object from a document.

    Args:
        doc_name: The name of the document to get the object from.
        obj_name: The name of the object to get.
        with_screenshot: Attach a screenshot of the object (default: no screenshot).

    Returns:
        The object properties, and a screenshot only when requested.
    """
    return get_object_operation(
        get_freecad_connection(),
        state.resolve_screenshot(with_screenshot),
        doc_name,
        obj_name,
    )


@mcp.tool()
def list_documents(ctx: Context) -> list[TextContent]:
    """Get the list of open documents in FreeCAD.

    Returns:
        A list of document names.
    """
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
    """Start a modeling session bound to a document.

    While a session is active, every successful cad() mutation on the session's
    document is recorded as a step backed by a FreeCAD transaction, enabling
    session_rollback for trial-and-error modeling. execute_code steps are
    recorded as non-atomic (rollback past them is blocked unless forced).

    Args:
        name: Optional human-readable session name.
        create_document: Create the document first if it does not exist.

    Returns:
        Session info including session_id.
    """
    return session_start_operation(get_freecad_connection(), doc_name, name, create_document)


@mcp.tool()
def session_status(ctx: Context) -> list[TextContent]:
    """Show the active session: step count, document state, next-step
    suggestions, and risks (e.g. state drift from GUI edits, non-atomic steps).

    Returns:
        JSON summary with a human-readable display_text.
    """
    return session_status_operation(get_freecad_connection())


@mcp.tool()
def session_get_steps(ctx: Context) -> list[TextContent]:
    """Return all recorded steps and notes of the active session.

    Returns:
        JSON list of steps (step_number, operation, description, params,
        objects_after fingerprint, atomic flag, timestamp).
    """
    return session_get_steps_operation()


@mcp.tool()
def session_rollback(
    ctx: Context,
    to_step: int,
    force: bool = False,
) -> list[TextContent]:
    """Roll back the model to a previous step.

    Undoes the corresponding document transactions in FreeCAD and truncates
    the step log. Removed steps go to a redo buffer (session_redo) until a new
    cad() operation discards them.

    Args:
        to_step: Keep steps 1..to_step; undo everything after (0 = undo all).
        force: Roll back even across non-atomic execute_code steps (risky:
            undo may revert the wrong change).

    Returns:
        JSON with undone count, removed step numbers, post-rollback object
        list, and a fingerprint consistency check.
    """
    return session_rollback_operation(get_freecad_connection(), to_step, force)


@mcp.tool()
def session_redo(ctx: Context, n: int = 1) -> list[TextContent]:
    """Redo n previously rolled-back steps (valid until a new cad() operation).

    Args:
        n: Number of steps to restore (default 1).

    Returns:
        JSON with restored step numbers.
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
        note: The note content.
        note_type: "observation" | "assumption" | "limitation" | "correction".

    Returns:
        The recorded note entry.
    """
    return session_add_note_operation(note, note_type)


@mcp.tool()
def session_pause(ctx: Context) -> list[TextContent]:
    """Pause the active session (persisted to disk; resume later).

    Returns:
        Confirmation with the session_id.
    """
    return session_pause_operation()


@mcp.tool()
def session_resume(ctx: Context, session_id: str) -> list[TextContent]:
    """Resume a paused/completed session from disk.

    Args:
        session_id: The session to resume (see session_list).

    Returns:
        Session state; warns if the bound document is no longer open.
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
    """Complete the active session: store the whole workflow as a reusable
    pattern in the pattern store (recall later with recall_patterns), and
    optionally save the document to disk.

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
    """Store a reusable modeling pattern (code snippet or workflow) into the
    pattern memory. Call this after a non-trivial approach worked.

    Knowledge hierarchy: ① your own knowledge first → ② recall_patterns when
    unsure → ③ inspect_freecad for API details. Successful new approaches
    should be stored back here.

    Args:
        name: Short pattern name (e.g. "flanged pipe via loft").
        description: What it does and when to use it.
        code: Optional Python snippet that implements it.
        tags: Retrieval tags.

    Returns:
        The stored pattern_id.
    """
    return save_pattern_operation(name, description, code, tags)


@mcp.tool()
def recall_patterns(
    ctx: Context,
    query: str,
    limit: int = 3,
) -> list[TextContent]:
    """Search the pattern memory for workflows/code similar to your task.

    Use when your own knowledge is insufficient, before falling back to
    trial-and-error. Patterns come from save_pattern and completed sessions.

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
    """Runtime introspection of the FreeCAD Python API (last-resort reference
    when both your knowledge and recall_patterns are insufficient).

    Two modes:
    - Object mode: pass doc_name + obj_name → the object's TypeId, settable
      properties (with types), public methods, and docstring.
    - API mode: pass dotted_name (e.g. "Part.makeLoft") → its docstring, or
      the member list of a module/class.

    Returns:
        JSON with properties/members/docstring (compact, capped).
    """
    return inspect_freecad_operation(get_freecad_connection(), doc_name, obj_name, dotted_name)


@mcp.tool()
def measure_geometry(ctx: Context, doc_name: str, obj_name: str) -> list[TextContent]:
    """Measure an object's Shape: volume, area, bounding box, center of mass,
    element counts (solids/faces/edges/vertices), and validity (is_valid).

    Use after modeling steps to verify design targets quantitatively.

    Args:
        doc_name: Document name.
        obj_name: Object name (must have a Shape).

    Returns:
        JSON with volume_mm3, area_mm2, bbox, center_of_mass, counts,
        is_valid, shape_type.
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
    """List an object's faces or edges with semantic info for selection.

    Faces sorted by area, edges by length, vertices by distance from origin
    (largest/longest/farthest first). Use the index/name (Face1, Edge3, ...)
    in follow-up operations such as fillet, boolean, or sketching on a face.

    Args:
        element: "faces", "edges", or "vertices".
        limit: Max entries returned (1-200, default 50).
        offset: Skip this many entries (pagination).

    Returns:
        JSON with total, returned, and a faces/edges list (index, name,
        type, area/length, center, normal for planar faces).
    """
    return get_topology_operation(
        get_freecad_connection(), doc_name, obj_name, element, limit, offset
    )


@mcp.tool()
def check_interference(ctx: Context, doc_name: str, obj_a: str, obj_b: str) -> list[TextContent]:
    """Check the spatial relationship between two objects: distance and
    intersection (common volume). Use to verify clearance or detect
    collisions in multi-body configurations.

    Args:
        doc_name: Document name.
        obj_a: First object name.
        obj_b: Second object name.

    Returns:
        JSON with distance_mm, intersects, common_volume_mm3.
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
    """Get detailed global-coordinate spatial info for a specific face, edge, or vertex.

    Returns center, normal, axis, radius, start/end points etc. in GLOBAL
    coordinates (already transformed by the object's Placement). Use this
    instead of get_topology when you need precise positioning data for
    alignment or assembly.

    Args:
        element: "face", "edge", or "vertex".
        element_index: 0-based index (use get_topology to find indices).

    Returns:
        JSON with global center, normal, axis, radius, endpoints, and object Placement.
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

    Modes: "touch" (face-to-face, normals opposing), "center" (centers
    coincide, translation only), "axis" (cylindrical axes aligned).
    offset: extra distance along target normal after alignment ("touch" only).

    Args:
        element / element_index: Element on the object to move.
        target_element / target_element_index: Element on the target.
        mode: Alignment mode: "touch", "center", or "axis".
        offset: Extra distance along target normal (positive = away).

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
    """List an object's assembly anchors in GLOBAL coordinates (read-only).

    Auto-derives standard anchors from the Shape (bbox_center/min/max, com,
    axis_mid/start/end for the dominant cylindrical face, face0..2_center for
    the largest planar faces) and merges explicit named anchors defined via
    set_anchors (explicit wins on a name clash). Call this BEFORE placing
    parts and plan mates from the returned numbers — never guess coordinates.

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
    """Define explicit named anchors on an object.

    Stored on the object (persists with the document) and follows Placement
    moves. coord_frame="global" converts document coords to local via the
    inverse Placement — use it whenever your source coordinates are global.

    Args:
        anchors: {name: {"pos": [x, y, z], "dir": [x, y, z] | null}}.
        replace: Replace all existing anchors instead of merging.
        coord_frame: "local" (stored as-is) or "global".
        with_screenshot: Attach a screenshot of the result (default: no screenshot).

    Returns:
        JSON with anchor_count; records a modeling-session step.
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
    """Assemble parts by snapping named anchors together (ONE transaction).

    Each mate: {"obj", "anchor", "target", "target_anchor",
                "mode": "center"|"touch"|"axis", "offset": float=0}.
    Per-mate residuals (mm, plus degrees for touch/axis) are measured AFTER
    the move; a mate over tolerance fails. For PERSISTENT joints use
    assembly_session. Full semantics: operation_help("assemble").

    Args:
        mates: Non-empty list of mate dicts (see above).
        tolerance: Max allowed post-move residual in mm (default 0.1).
        stop_on_error: Abort and roll back at the first failed mate.
        with_screenshot: Attach a screenshot (default: none).

    Returns:
        JSON with per-mate residuals and passed/failed counts.
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
    """Audit the document's spatial sanity (read-only, pure data feedback).

    Reports: floating (nearest neighbour farther than float_threshold mm),
    interferences (common volume over interference_min_volume mm3), and
    per-check pass/fail for requested anchor pairs {"obj", "anchor",
    "target", "target_anchor", "tolerance"?}. Call after modeling/assembly
    steps for a numeric health report instead of eyeballing screenshots.

    Args:
        checks: Optional anchor-pair distance checks (see above).
        float_threshold: Nearest-neighbour gap (mm) for "floating" (default 1.0).
        interference_min_volume: Minimum common volume (mm3) to report.

    Returns:
        JSON with floating/interferences/checks lists and a summary.
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

    Workflow: start(ground=part) -> add_component(part) per part ->
    mate(a, b, joint_type, trim?) per joint -> solve -> verify -> complete.
    Joints persist in the document: move a parent part, call solve, and
    children follow. rollback(to_step) un-does joints/trims and restores
    placements atomically.

    A mate ref is {"part": <name>} plus exactly ONE of face="FaceN" /
    anchor=<name> / point=[x,y,z]. Full reference (operations, joint types,
    trim semantics): operation_help("assembly_session").
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
    mcp.run()
