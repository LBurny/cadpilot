import json
import logging
import os
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from pydantic import Field

try:
    # mcp 1.x
    from mcp.server.fastmcp import Context, FastMCP
except ImportError:
    # mcp 2.x moved mcp.server.fastmcp to mcp.server.mcpserver and renamed
    # FastMCP to MCPServer; the API surface used here is unchanged.
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver import MCPServer as FastMCP
from mcp.types import TextContent

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
    session_action_operation,
    set_anchors_operation,
    step_control_operation,
    step_plan_operation,
    verify_assembly_operation,
)
from .pattern_store import get_pattern, list_patterns
from .prompt_text import ASSET_CREATION_STRATEGY
from .responses import text_response
from .server_state import ServerState
from .tool_docs import operation_help_text

logging.basicConfig(
    level=logging.WARNING, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)


def _resolve_mcp_log_level(raw: str | None) -> str:
    """A valid level name from ``CADPILOT_LOG_LEVEL``; INFO when unusable.

    ``logger.setLevel`` raises ValueError on an unknown name, so this has to be
    validated rather than passed straight through; a typo in the environment
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


# --- tool annotations (mcp-builder) -----------------------------------------
# Every tool advertises the four MCP hints so a client can tell read-only
# introspection from a document mutation without guessing. openWorldHint is
# True wherever the call reaches the FreeCAD process over XML-RPC, an entity
# outside this server; the purely local knowledge/pattern tools are closed.


def _read_only(title: str, *, open_world: bool = True) -> dict[str, Any]:
    return {
        "title": title,
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": open_world,
    }


def _mutating(title: str, *, destructive: bool = True) -> dict[str, Any]:
    return {
        "title": title,
        "readOnlyHint": False,
        "destructiveHint": destructive,
        "idempotentHint": False,
        "openWorldHint": True,
    }


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
# unattended, so a wedged addon still leaves its last activity visible in
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


@mcp.tool(annotations=_mutating("Create Document", destructive=False))
def create_document(
    ctx: Context,
    name: Annotated[str, Field(min_length=1)],
) -> list[TextContent]:
    """Create a new FreeCAD document.

    Args:
        name: Document name. FreeCAD may sanitize or deduplicate it, so read the actual name from the result.
    """
    return create_document_operation(
        get_freecad_connection(),
        name,
    )


@mcp.tool(annotations=_mutating("CAD Modeling Operation"))
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
    ],
    doc_name: str,
    obj_type: str | None = None,
    obj_name: str | None = None,
    obj_properties: dict[str, Any] | None = None,
    ops: list[dict[str, Any]] | None = None,
    stop_on_error: bool = False,
    description: str = "",
) -> list[TextContent]:
    """Run one CAD modeling operation; the unified mutation entry point.

    obj_name is the BASE object, except for loft/sketch/variables/datum_plane/hull (the NEW object); params go in obj_properties. Committed mutations are rollback-able session steps.

    Args:
        obj_type: Object type for create_object, e.g. "Part::Box".
        ops: Operation dicts for batch.
        stop_on_error: Stop a batch at the first failure.
        description: One-line purpose of the step; the Steps panel's row label.

    Reference: operation_help("<operation>").
    """
    return cad_operation(
        get_freecad_connection(),
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


@mcp.tool(annotations=_mutating("Execute Code (Background)"))
def execute_code_async(ctx: Context, code: str) -> list[TextContent]:
    """Run Python on a background thread and return a task_id; poll the result with get_task_result.

    The code must not touch the GUI: no FreeCADGui, views or selection, document objects, recompute or save; use execute_code for those. Output goes through task_print(...); print() is not captured.

    Args:
        code: Background-safe Python code.
    """
    return execute_code_async_operation(get_freecad_connection(), code)


@mcp.tool(annotations=_read_only("Get Async Task Result"))
def get_task_result(
    ctx: Context, task_id: Annotated[str, Field(min_length=1)]
) -> list[TextContent]:
    """Poll the status and captured output of one execute_code_async task.

    Args:
        task_id: Task ID returned by execute_code_async.

    Returns: JSON {status: running | done | error, output, traceback}.
    """
    return get_task_result_operation(get_freecad_connection(), task_id)


@mcp.tool(annotations=_mutating("Execute Code (GUI Thread)"))
def execute_code(
    ctx: Context,
    code: str,
    doc_name: str | None = None,
) -> list[TextContent]:
    """Run Python in FreeCAD's GUI thread (FreeCAD/App, FreeCADGui, Part pre-imported); print() output is returned.

    The run is transactional: a mutating snippet commits as one undoable step, a read-only run is not recorded.

    Args:
        code: Code to run; start with a # comment naming the step, which becomes the step's row label and description.
        doc_name: Bind the transaction, step journal and App.ActiveDocument to this document; defaults to the session's document, else the process's home document.
    """
    return execute_code_operation(
        get_freecad_connection(),
        code,
        doc_name=doc_name,
    )


@mcp.tool(annotations=_read_only("Capture View Screenshot"))
def get_view(
    ctx: Context,
    view_name: Literal[
        "Isometric", "Front", "Top", "Right", "Back", "Left", "Bottom", "Dimetric", "Trimetric"
    ],
    width: Annotated[int, Field(ge=16, le=8192)] | None = None,
    height: Annotated[int, Field(ge=16, le=8192)] | None = None,
    focus_object: str | None = None,
    doc_name: str | None = None,
) -> list[TextContent]:
    """Capture one document's view and return the saved PNG's path.

    Costly in context: reserve it for visual checks; prefer get_objects or measure_geometry for numbers. Without width/height the long edge is capped at 384 px.

    Args:
        view_name: Camera view, one of the nine names.
        width/height: Pixel size; a smaller image saves context.
        focus_object: Fit this object; the default frames everything.
        doc_name: Frame this document; another agent may have switched the foreground tab.
    """
    if state.only_text_feedback:
        return text_response("Screenshots are disabled by --only-text-feedback.")
    return get_view_operation(
        get_freecad_connection(), view_name, width, height, focus_object, doc_name
    )


@mcp.tool(annotations=_read_only("List Objects / Get Object"))
def get_objects(
    ctx: Context,
    doc_name: Annotated[str, Field(min_length=1)],
    obj_name: str | None = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
    offset: Annotated[int, Field(ge=0, le=10_000_000)] = 0,
) -> list[TextContent]:
    """List a document's objects, or read one object's full properties.

    Args:
        obj_name: Omit to list the objects; pass a name to read that object instead.
        limit/offset: Page size and start index for the object list.
    """
    return get_objects_operation(
        get_freecad_connection(),
        doc_name,
        obj_name,
        limit,
        offset,
    )


@mcp.tool(annotations=_read_only("List Documents"))
def list_documents(
    ctx: Context,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
    offset: Annotated[int, Field(ge=0, le=10_000_000)] = 0,
) -> list[TextContent]:
    """List the open FreeCAD documents.

    Args:
        limit/offset: Page size and start index.
    """
    return list_documents_operation(get_freecad_connection(), limit, offset)


# ---------------------------------------------------------------------------
# Modeling sessions (step recording + rollback), pattern memory, introspection
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_mutating("Modeling Session"))
def session(
    ctx: Context,
    action: Literal[
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
    ],
    doc_name: str | None = None,
    session_id: str = "",
    name: str = "",
    create_document: bool = False,
    to_step: Annotated[int, Field(ge=0, le=1_000_000)] | None = None,
    n: Annotated[int, Field(ge=1)] = 1,
    force: bool = False,
    note: str = "",
    note_type: str = "observation",
    save: bool = False,
    save_path: str | None = None,
    description: str = "",
    tags: list[str] | None = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
    offset: Annotated[int, Field(ge=0, le=10_000_000)] = 0,
) -> list[TextContent]:
    """A modeling session bound to a document; cad() mutations become transaction-backed steps you can roll back or redo.

    Actions: start (doc_name, create_document?) | status | get_steps (limit/offset) | rollback (to_step, force?) | redo (n?) | add_note (note, note_type?) | pause | resume (session_id) | list | complete (save?, save_path?, description?, tags?).

    Reference: operation_help("session").
    """
    return session_action_operation(
        get_freecad_connection(),
        action,
        doc_name=doc_name,
        session_id=session_id,
        name=name,
        create_document=create_document,
        to_step=to_step,
        n=n,
        force=force,
        note=note,
        note_type=note_type,
        save=save,
        save_path=save_path,
        description=description,
        tags=tags,
        limit=limit,
        offset=offset,
    )


@mcp.tool(annotations=_mutating("Plan Modeling Steps", destructive=False))
def step_plan(
    ctx: Context,
    doc_name: str,
    steps: list[dict[str, Any]],
    description: str = "",
) -> list[TextContent]:
    """Submit a modeling plan to the document's step journal without executing it.

    Each step is a cad() argument dict and waits for release in the CADPilot Steps panel or via step_control.

    Reference: operation_help("step_plan").
    """
    return step_plan_operation(get_freecad_connection(), doc_name, steps, description)


@mcp.tool(annotations=_mutating("Control Step Journal"))
def step_control(
    ctx: Context,
    doc_name: str,
    action: str,
    index: Annotated[int, Field(ge=0)] = 0,
    params: dict[str, Any] | None = None,
    force: bool = False,
    confirm: bool = False,
) -> list[TextContent]:
    """Review, run and undo the steps of a document's step journal.

    Actions: run_next | run_all | run_to | rollback_to | reexecute | accept | reject | update | insert | replay | snapshot | clear_plan | reset (needs confirm=true) | status.

    Reference: operation_help("step_control").
    """
    return step_control_operation(
        get_freecad_connection(), doc_name, action, index, params, force, confirm
    )


@mcp.tool(annotations=_read_only("Read Addon Log"))
def get_addon_log(
    ctx: Context,
    level: str = "INFO",
    grep: str = "",
    since_seq: Annotated[int, Field(ge=0, le=1_000_000_000)] = 0,
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
) -> list[TextContent]:
    """Read the addon's ring-buffer debug log (newest last) while FreeCAD is wedged or a call misbehaves.

    Args:
        level: Minimum level: DEBUG, INFO, WARNING or ERROR.
        grep: Substring filter over message and detail.
        since_seq: Only records after this sequence number.
        limit: Max records (default 100).

    Reference: operation_help("get_addon_log").
    """
    return get_addon_log_operation(
        get_freecad_connection(), level or None, grep or None, since_seq, limit
    )


@mcp.tool(annotations=_mutating("Diagnose Connection", destructive=False))
def diagnose(ctx: Context, host: str | None = None, dismiss: bool = False) -> list[TextContent]:
    """Find out why CADPilot cannot reach FreeCAD; works while FreeCAD is down.

    Probes the RPC port, the process, the addon install and its logs.

    Args:
        host: FreeCAD host to probe; defaults to this server's --host.
        dismiss: Also close the modal dialog blocking every call (Cancel, nothing confirmed).

    Reference: operation_help("diagnose").
    """
    # The connection is resolved ONLY for the acting call: a plain diagnose must
    # not touch FreeCAD (it exists to answer while FreeCAD is down). It is also
    # resolved DEFENSIVELY — get_freecad_connection() RAISES when the endpoint is
    # down (it does not answer None), and letting that escape would lose the very
    # report the caller needs exactly when FreeCAD is unreachable. The operation
    # reports a None connection as "Dismiss: skipped (no connection available to
    # act on)".
    connection = None
    if dismiss:
        try:
            connection = get_freecad_connection()
        except Exception as e:
            logger.info("diagnose: no connection to dismiss with (%s)", e)
    return diagnose_operation(host or state.rpc_host, dismiss=dismiss, freecad=connection)


@mcp.tool(annotations=_mutating("Save Pattern", destructive=False))
def save_pattern(
    ctx: Context,
    name: Annotated[str, Field(min_length=1)],
    description: Annotated[str, Field(min_length=1)],
    code: str = "",
    tags: list[str] | None = None,
) -> list[TextContent]:
    """Store a reusable modeling pattern for later recall; call this after a non-trivial approach worked.

    Args:
        name: Short name, e.g. "flanged pipe via loft".
        description: What it does and when to use it.
        code: Optional Python snippet that implements it.
        tags: Retrieval tags.

    Returns: The stored pattern_id.
    """
    return save_pattern_operation(name, description, code, tags)


@mcp.tool(annotations=_read_only("Recall Patterns", open_world=False))
def recall_patterns(
    ctx: Context,
    query: str,
    limit: Annotated[int, Field(ge=1, le=50)] = 3,
) -> list[TextContent]:
    """Search the pattern memory for workflows or code like your task; do this before trial and error when your knowledge may not cover it.

    Args:
        query: Keywords describing the task, e.g. "boolean cut holes cylinder".
        limit: Max results (default 3).

    Returns: JSON list of matching patterns with code/steps.
    """
    return recall_patterns_operation(query, limit)


@mcp.tool(annotations=_read_only("Operation Reference Help", open_world=False))
def operation_help(ctx: Context, operation: str | None = None) -> list[TextContent]:
    """Full parameter reference for a cad() operation or another topic. Call it with an operation name such as "sketch", or with no argument for the topic list."""
    return operation_help_operation(operation)


@mcp.tool(annotations=_read_only("Inspect FreeCAD API"))
def inspect_freecad(
    ctx: Context,
    doc_name: str | None = None,
    obj_name: str | None = None,
    dotted_name: str | None = None,
) -> list[TextContent]:
    """Inspect the live FreeCAD Python API; the last resort when your own knowledge and recall_patterns are insufficient.

    With doc_name and obj_name you get an object's TypeId, settable properties, methods and docstring. With dotted_name (e.g. "Part.makeLoft") you get a docstring or a member list.
    """
    return inspect_freecad_operation(get_freecad_connection(), doc_name, obj_name, dotted_name)


@mcp.tool(annotations=_read_only("Measure Geometry"))
def measure_geometry(ctx: Context, doc_name: str, obj_name: str) -> list[TextContent]:
    """Measure an object's Shape (the object must have one) to verify a design target numerically.

    Returns: JSON volume_mm3, area_mm2, bbox, center_of_mass, element counts, is_valid, shape_type. Numbers carry 6 significant digits; the bbox is OCC's bound box, exact for planar shapes and within about 0.1 mm on curved ones.
    """
    return measure_geometry_operation(get_freecad_connection(), doc_name, obj_name)


@mcp.tool(annotations=_read_only("Get Topology"))
def get_topology(
    ctx: Context,
    doc_name: str,
    obj_name: str,
    element: Literal["faces", "edges", "vertices"] = "faces",
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    offset: Annotated[int, Field(ge=0, le=10_000_000)] = 0,
) -> list[TextContent]:
    """List an object's topology for selection: faces by area, edges by length, vertices by distance, largest first. The names (Face1, Edge3, ...) feed fillet, boolean, sketch-on-face and friends.

    Args:
        element: Which kind to list: faces, edges or vertices.
        limit: Max entries (1 to 200, default 50).
        offset: How many entries to skip.

    Returns: total, returned, and a '{element}' list of {index, name, type, area or length, center, planar-face normal}.
    """
    return get_topology_operation(
        get_freecad_connection(), doc_name, obj_name, element, limit, offset
    )


@mcp.tool(annotations=_read_only("Check Interference"))
def check_interference(ctx: Context, doc_name: str, obj_a: str, obj_b: str) -> list[TextContent]:
    """Check two objects for interference: the distance between them and their common volume. Use it to verify clearance or detect collisions.

    Returns: JSON distance_mm, intersects, common_volume_mm3.
    """
    return check_interference_operation(get_freecad_connection(), doc_name, obj_a, obj_b)


@mcp.tool(annotations=_read_only("Get Positioning Info"))
def get_positioning_info(
    ctx: Context,
    doc_name: str,
    obj_name: str,
    element: Literal["face", "edge", "vertex"],
    element_index: Annotated[int, Field(ge=0, le=1_000_000)],
) -> list[TextContent]:
    """One face, edge or vertex in global coordinates: center, normal, axis, radius, endpoints; the Placement is already applied. Prefer it over get_topology for precise positioning before alignment or assembly.

    Args:
        element: Which kind: face, edge or vertex.
        element_index: 0-based index; call get_topology to find the indices.
    """
    return get_positioning_info_operation(
        get_freecad_connection(), doc_name, obj_name, element, element_index
    )


@mcp.tool(annotations=_mutating("Align Shapes"))
def align_shapes(
    ctx: Context,
    doc_name: str,
    obj_name: str,
    element: Literal["face", "edge", "vertex"],
    element_index: Annotated[int, Field(ge=0, le=1_000_000)],
    target_obj: str,
    target_element: Literal["face", "edge", "vertex"],
    target_element_index: Annotated[int, Field(ge=0, le=1_000_000)],
    mode: Literal["touch", "center", "axis"] = "touch",
    offset: Annotated[float, Field(allow_inf_nan=False)] = 0.0,
) -> list[TextContent]:
    """Move an object so the chosen element aligns with an element of the target.

    Args:
        element / element_index: Element on the object to move.
        target_element / target_element_index: Element on the target.
        mode: touch (face to face, normals opposing), center (centers coincide) or axis (cylindrical axes aligned).
        offset: Extra distance along the target normal; positive moves away.
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


@mcp.tool(annotations=_read_only("Get Anchors"))
def get_anchors(ctx: Context, doc_name: str, obj_name: str) -> list[TextContent]:
    """List an object's assembly anchors in GLOBAL coordinates. Auto-derived ones (bbox_center/min/max, com, axis_mid/start/end for the dominant cylindrical face, face0..2_center for the largest planar faces) merge with set_anchors ones, and explicit wins. Call it before placing parts and plan mates from the returned numbers; never guess coordinates.

    Returns: JSON with anchors as {name: {pos, dir, source: "auto" | "explicit"}}.
    """
    return get_anchors_operation(get_freecad_connection(), doc_name, obj_name)


@mcp.tool(annotations=_mutating("Set Anchors"))
def set_anchors(
    ctx: Context,
    doc_name: str,
    obj_name: str,
    anchors: dict[str, Any],
    replace: bool = False,
    coord_frame: Literal["local", "global"] = "local",
) -> list[TextContent]:
    """Define named anchors on an object. They persist with the document, move with its Placement, and the write becomes a modeling-session step.

    Args:
        anchors: {name: {"pos": [x, y, z], "dir": [x, y, z] | null}}.
        replace: Replace all existing anchors instead of merging.
        coord_frame: local stores values as given; global converts them first, for global source coordinates.
    """
    return set_anchors_operation(
        get_freecad_connection(),
        doc_name,
        obj_name,
        anchors,
        replace,
        coord_frame,
    )


@mcp.tool(annotations=_mutating("Assemble (Snap Anchors)"))
def assemble(
    ctx: Context,
    doc_name: str,
    mates: list[dict[str, Any]],
    tolerance: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0.1,
    stop_on_error: bool = True,
) -> list[TextContent]:
    """Snap parts together by matching named anchors in one transaction. For persistent joints use assembly_session.

    Args:
        mates: Non-empty list of mate dicts.
        tolerance: Maximum allowed post-move residual in mm (default 0.1).
        stop_on_error: Stop at the first failed mate (mates already applied stay; nothing moves only if the FIRST mate fails).

    Reference: operation_help("assemble").
    """
    return assemble_operation(
        get_freecad_connection(),
        doc_name,
        mates,
        tolerance,
        stop_on_error,
    )


@mcp.tool(annotations=_read_only("Verify Assembly"))
def verify_assembly(
    ctx: Context,
    doc_name: str,
    checks: list[dict[str, Any]] | None = None,
    float_threshold: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 1.0,
    interference_min_volume: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 1.0,
) -> list[TextContent]:
    """Audit a document's spatial health, read-only: floating parts, interferences, and anchor-pair distances. Hidden objects are skipped. Trust these numbers over a screenshot.

    Args:
        checks: Optional anchor-pair distance checks.
        float_threshold: Gap in mm above which a part counts as floating (default 1.0).
        interference_min_volume: Smallest common volume in mm3 worth reporting.

    Returns: JSON floating/interferences/checks lists and a summary.
    """
    return verify_assembly_operation(
        get_freecad_connection(),
        doc_name,
        checks,
        float_threshold,
        interference_min_volume,
    )


@mcp.tool(annotations=_mutating("Assembly Session (Joints)"))
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
    to_step: Annotated[int, Field(ge=0, le=1_000_000)] | None = None,
    gap_samples: Annotated[int, Field(ge=2, le=64)] = 8,
) -> list[TextContent]:
    """Persistent-joint assembly on FreeCAD's Assembly workbench; prefer it over the one-shot assemble when joints must survive later moves.

    Workflow: start(ground=part), add_component(part), mate(a, b, joint_type, trim?), solve, verify, complete. Moving a parent part and re-solving moves the children. rollback(to_step) restores placements atomically. A mate ref is {"part": <name>} plus exactly one of face="FaceN", anchor=<name> or point=[x,y,z].

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


# --- resources (mcp-builder: expose semi-static data without a tool call) ----
# The long operation reference is otherwise reachable only through
# operation_help; as a resource it can be pulled in by URI, and the pattern
# memory becomes browsable read-only. Both are cheap local reads.


@mcp.resource(
    "cadpilot://operations",
    name="operation-index",
    title="CADPilot operation reference index",
    description="Every cad() operation and tool topic served by operation_help.",
    mime_type="text/markdown",
)
def operations_index_resource() -> str:
    return operation_help_text(None)


@mcp.resource(
    "cadpilot://docs/{operation}",
    name="operation-doc",
    title="CADPilot operation reference",
    description="Full parameter reference for one cad() operation or tool topic.",
    mime_type="text/markdown",
)
def operation_doc_resource(operation: str) -> str:
    return operation_help_text(operation)


@mcp.resource(
    "cadpilot://patterns",
    name="pattern-memory",
    title="CADPilot pattern memory",
    description="Stored reusable modeling patterns (id, name, description, tags).",
    mime_type="application/json",
)
def patterns_resource() -> str:
    entries = [
        {k: p.get(k) for k in ("pattern_id", "name", "description", "tags")}
        for p in list_patterns(limit=200)
    ]
    return json.dumps({"count": len(entries), "patterns": entries}, ensure_ascii=False, default=str)


@mcp.resource(
    "cadpilot://patterns/{pattern_id}",
    name="pattern",
    title="CADPilot stored pattern",
    description="One stored pattern, including its code/steps.",
    mime_type="application/json",
)
def pattern_resource(pattern_id: str) -> str:
    entry = get_pattern(pattern_id)
    if entry is None:
        return json.dumps({"found": False, "pattern_id": pattern_id})
    return json.dumps(entry, ensure_ascii=False, default=str)


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
    state.rpc_host = args.host
    state.auto_audit = not args.no_auto_audit
    logger.info(f"Only text feedback: {state.only_text_feedback}")
    logger.info(f"Auto connectivity audit: {state.auto_audit}")
    logger.info(f"Connecting to FreeCAD RPC server at: {state.rpc_host}")
    _maybe_start_log_forwarder()
    mcp.run()
