# CADPilot — Workspace Guide

## Purpose

MCP (Model Context Protocol) server that lets AI clients (Claude Desktop, LangChain, etc.) control FreeCAD remotely. Two main components communicate over XML-RPC:

1. **MCP server** (`src/cadpilot/`) — Python package run via `uvx cadpilot` or `uv run cadpilot`. Speaks MCP to the AI client and XML-RPC to FreeCAD.
2. **FreeCAD addon** (`addon/CADPilot/`) — Installed into FreeCAD's `Mod/` directory. Hosts the XML-RPC server inside the FreeCAD process and dispatches all document/GUI work onto the main thread.

## Directory Layout

```
src/cadpilot/          # MCP server package (published to PyPI)
  server.py               # FastMCP tool definitions & CLI entry point (main())
  freecad_client.py       # XML-RPC client proxy to FreeCAD addon
  operations/core.py      # Tool operation implementations (one function per tool)
  operations/__init__.py  # Re-exports all operations
  responses.py            # ToolResponse type alias, text/json/screenshot helpers
  server_state.py         # ServerState dataclass (connection, host, screenshot flags)
  session_state.py        # ModelingSession/Step dataclasses, JSON persistence, current-session registry
  assembly_state.py       # AssemblySession/AssemblyStep dataclasses, JSON persistence (~/.cadpilot/assembly/), precomputed-undo rollback
  operations/assembly.py  # assembly_session tool: spec validation → RPC assembly_op → session recording
  pattern_store.py        # Pattern memory (reusable workflows), keyword retrieval
  guidance.py             # Lightweight next-step suggestions & risk heuristics (incl. primitive_without_sketch / absolute_placement / assembly-mode steer)
  prompt_text.py          # ASSET_CREATION_STRATEGY prompt template
  tool_docs.py            # Long per-operation reference docs served by the operation_help tool

addon/CADPilot/             # FreeCAD workbench addon (copied to FreeCAD's Mod/ dir)
  InitGui.py              # Workbench registration, toolbar/menu, auto-start
  Init.py                 # Path setup
  rpc_server/
    rpc_server.py         # FreeCADRPC class — XML-RPC handler, server start/stop
    gui_dispatch.py       # dispatch_to_gui() — queues work onto the GUI thread
    commands.py           # FreeCAD Command classes for toolbar buttons
    object_factory.py     # create_object_gui() — object creation logic
    property_mapper.py    # set_object_property() — recursive property assignment
    serialize.py          # serialize_object() — object → dict for RPC responses
    view_manager.py       # save_active_screenshot() — camera/view screenshot logic
    geometry_query.py     # Read-only Shape queries — measure/topology/interference
    assembly_ops.py       # Anchor-based assembly — anchors (auto/explicit), assemble (mates), verify_assembly
    joint_ops.py          # Persistent-joint assembly — FreeCAD 1.1 Assembly WB lifecycle (Link wrap, joints, preSolve+solve, rollback, verify+gap profile)
    trim_ops.py           # Declarative priority trimming — non-destructive baked cut in loser-link frame
    feature_ops.py        # Parametric feature creation (boolean/fillet/sketch/pad/...) + selectors
    sketcher_ops.py       # Constrained-sketch builder (geometry/constraints/solver diagnostics)
    ip_filter.py          # FilteredXMLRPCServer — IP/CIDR allowlist
    settings.py           # JSON settings persistence (auto-start, remote, allowed IPs)

examples/                 # Usage examples (adk/agent.py, langchain/react.py)
tests/                    # pytest suite for the MCP server side (fake XML-RPC connection)
assets/                   # Demo GIFs and images for README
```

## Build & Run

```bash
# Install dependencies (requires Python ≥3.12, uv)
uv sync

# Run MCP server locally (developer mode)
uv run cadpilot                          # connects to FreeCAD on localhost:9875
uv run cadpilot --with-screenshots       # attach screenshots by default (multimodal models)
uv run cadpilot --only-text-feedback     # never return screenshots (hard guarantee)
uv run cadpilot --host 192.168.1.100     # connect to remote FreeCAD

# Publish to PyPI (via hatchling)
uv build

# Run tests (MCP server side only; the addon needs a live FreeCAD)
uv run pytest
```

The FreeCAD addon must be installed separately — copy `addon/CADPilot/` into FreeCAD's `Mod/` directory and restart FreeCAD.

## Addon Hot-Reload (No Restart)

During development you can reload the addon **without restarting FreeCAD**:

1. Copy the updated addon files to the live `Mod/` directory:
   ```bash
   cp -rf "H:/My_Software/FreeCAD-MCP/addon/CADPilot/." \
          "C:/Users/intel/AppData/Roaming/FreeCAD/v1-1/Mod/CADPilot/"
   ```

2. In FreeCAD's Python console (or via `execute_code`) run:
   ```python
   import sys, importlib
   import rpc_server.rpc_server as rs_old

   print("stop:", rs_old.stop_rpc_server())

   from PySide import QtCore  # or PySide6 / PySide2


   def _start(rs):
       result = rs.start_rpc_server(9875)
       print("start:", result)
       if "still stopping" in str(result):
           # Previous stop is still draining; retry instead of giving up
           # (giving up here leaves a half-restarted server: socket dead,
           # no heartbeat, every GUI-dispatched call hangs).
           QtCore.QTimer.singleShot(4000, lambda: _start(rs))


   def restart():
       for sub in [
           "ip_filter",
           "settings",
           "gui_dispatch",
           "object_factory",
           "property_mapper",
           "serialize",
           "view_manager",
           "commands",
           "geometry_query",
           "assembly_ops",
           "trim_ops",
           "joint_ops",
           "sketcher_ops",
           "feature_ops",
       ]:
           name = f"rpc_server.{sub}"
           if name in sys.modules:
               importlib.reload(sys.modules[name])
       rs = importlib.reload(rs_old)
       _start(rs)


   # Deferred restart: the in-flight XML-RPC request blocks shutdown drain,
   # so we wait 4s for server_close() to finish before re-binding the port.
   QtCore.QTimer.singleShot(4000, restart)
   ```

3. Wait ~8 seconds before issuing the next MCP call so the new server is ready.

4. If GUI-dispatched calls (e.g. `execute_code`) hang after the reload but
   `ping` still answers, the heartbeat/waker chain died during the race.
   Repair via `execute_code_async` (its worker runs without GUI dispatch):
   ```python
   import rpc_server.gui_dispatch as gd
   from PySide import QtCore
   import FreeCADGui

   def repair():
       while not gd._rpc_request_queue.empty():
           t = gd._rpc_request_queue.get()
           if t is not gd._SHUTDOWN:
               t()
       QtCore.QTimer.singleShot(500, gd.process_gui_tasks)

   QtCore.QTimer.singleShot(0, FreeCADGui.getMainWindow(), repair)
   ```

> **Why deferred?** `stop_rpc_server()` calls `shutdown()` which blocks until the current request drains, and `server_close()` must release the socket before `start_rpc_server()` can bind again. Running the restart synchronously inside the same `execute_code` call deadlocks because the request itself is blocking shutdown. The `QTimer` deferral runs on FreeCAD's main GUI thread after the RPC call returns.

## Architecture & Key Constraints

- **GUI thread rule**: All FreeCAD document/GUI operations MUST run on FreeCAD's main (GUI) thread. The addon uses `dispatch_to_gui()` to queue lambdas and a `QTimer` waker to process them. Never call FreeCAD APIs directly from the RPC server thread.
- **Two-process model**: The MCP server and FreeCAD run in separate processes. They communicate exclusively via XML-RPC on port 9875. The MCP server never imports FreeCAD.
- **MCP version compatibility**: `server.py` imports `FastMCP` from `mcp.server.fastmcp` (1.x) with a fallback to `MCPServer` from `mcp.server.mcpserver` (2.x). Keep both paths working.
- **Timeouts**: Default XML-RPC transport timeout is 150s. `execute_code` has a 90s GUI-thread timeout; use `execute_code_async` for longer operations.
- **Reconnect**: `FreeCADConnection._invoke` rebuilds the XML-RPC proxy and retries once on dead-connection errors (FreeCAD/addon restart). Socket timeouts are NOT retried — the op may still be executing server-side.
- **Screenshot handling**: Screenshots are OPTIONAL and off by default. Per-call `with_screenshot` params opt in; `--with-screenshots` makes them default-on; `--only-text-feedback` is a hard off that overrides everything (see `ServerState.resolve_screenshot`). Screenshots are base64 PNG via temp files. Mutation tools (create/edit/delete/execute_operations) capture the screenshot inline in the same RPC dispatch; the client falls back to a second `get_active_screenshot` call against old addons. When no explicit size is given, the long edge is capped at 768px (`DEFAULT_MAX_DIM` in `view_manager.py`). Some view types (TechDraw, Spreadsheet) don't support screenshots — `get_active_screenshot` returns `None` in those cases.
- **Async tasks**: `execute_code_async` returns a `task_id`; status, `task_print()` output, and tracebacks are kept in a bounded in-memory registry (`_async_tasks`, FIFO max 50) and polled via `get_task_result`. `sys.stdout` is never redirected for background tasks (process-wide race).
- **Unified cad() tool**: All mutations go through `cad(operation=...)` (nsforge math()-style dispatcher) to keep the tool list small. Steps: the operation functions in `operations/core.py` remain the implementation; `cad_operation` dispatches and records session steps. Scope is modeling-only — FEM analysis, the parts library, and `reload_document` were removed in v0.2. Feature ops (boolean/fillet/chamfer/loft/sweep/mirror/pattern, plus Sketcher/PartDesign: variables/sketch/pad/pocket/revolution/groove/thickness/draft since v0.3, plus datum_plane/hull since v0.4) share the single RPC `create_feature` and pass params as a spec dict (obj_name = base, obj_properties = params). For `sketch`/`variables`/`datum_plane`/`hull`, obj_name names the NEW object and `spec["base"]` carries it (see `CAD_NO_BASE_OPERATIONS`). Sketches are built atomically in `sketcher_ops.py` (geometry + constraints in one transaction, solver runs immediately); results carry `dof`/`fully_constrained`/`warnings` via `describe_feature`, failures roll back with solver diagnostics. Thickness/draft support both the FreeCAD ≥1.1 LinkSub `Base` layout and the ≤1.0 `Faces` property (probe `PropertiesList`).
- **Sketch mode details** (live-verified on FreeCAD 1.1.3): sketch `external: [[obj, "EdgeN"|"VertexN"], ...]` adds external geometry (GeoIds from -3 in list order, start/end points ONLY — `mid`/`center` on external ids fails at solve with MalformedConstraints, so `_check_external_point_refs` rejects it up front); out-of-body targets are auto-bridged via `PartDesign::SubShapeBinder` (`_external_binder`, idempotent `Ext_<obj>_<elem>`) because PartDesign rejects external geometry outside the sketch's body, and `addExternal` takes `(str, str)`. `datum_plane` attaches a PartDesign::Plane to an origin plane (`body.Origin.OriginFeatures` Role lookup) or an existing face (FlatFace + optional offset); sketches attach via `plane={"datum": name}`. `hull` = visual hull: intersect 2-3 view-profile sketches extruded along their sketch normals (extent = union bbox projected per-normal ±`margin`, default max(1mm, 5% of diagonal)); result is a STATIC `Part::Feature` (no proxy — survives document reload), same-name re-run replaces the Shape in place (iterate), multiple solids → largest wins, empty intersection → RuntimeError. v1 limits: view sketches at the global origin, one closed outer profile per view. **Attachment fusion**: pad/pocket on a sketch attached to a solid's face (directly or through a datum plane) operate on THAT solid via attachment — pocket cuts it, pad fuses into it; do NOT pad-then-boolean-cut (the pad already contains the base solid). **execute_code + openTransaction**: always pair with try/except → `doc.abortTransaction()` — a leaked transaction leaves broken objects that poison later recomputes (null shapes).
- **Geometry sensing**: `measure_geometry`/`get_topology`/`check_interference` are read-only Shape queries (addon `geometry_query.py`), dispatched to the GUI thread without a transaction. Values are rounded to 4 significant digits; topology lists are size-sorted and paginated (`limit`/`offset`).
- **Assembly toolchain**: `get_anchors`/`set_anchors`/`assemble`/`verify_assembly` (addon `assembly_ops.py`) give the model data-driven spatial awareness — no screenshots required. Anchors are named points+directions: auto-derived per object (`bbox_center/min/max`, `com`, `axis_mid/start/end` from the dominant cylindrical face, `face0-2_center` from the largest planar faces) plus explicit named ones stored as JSON in an `App::PropertyString` named `MCP_Anchors` in LOCAL coords (they follow Placement). `set_anchors(coord_frame="global")` converts via `obj.Placement.inverse()` at write time — use it whenever the source coordinates are global, because many objects carry non-identity Placements. `assemble` takes a mate list `{obj, anchor, target, target_anchor, mode: center|touch|axis, offset}`, applies each mate in one FreeCAD transaction, then RE-RESOLVES the anchor post-move to report per-mate residuals (mm + deg) and aborts on `tolerance` violation; partial commit via `commit_if` when at least one mate passed. `verify_assembly` audits the whole document: floating objects (nearest-neighbour distance via bbox prefilter + `distToShape`), interferences (common volume for bbox-overlapping pairs), and explicit anchor-pair checks with per-check tolerance. PRECISION RULE: `_r()`/`_vec()` rounding (4 sig digits) applies ONLY at the report boundary — `_resolve_anchor`/`_auto_anchor_map` must return RAW `FreeCAD.Vector`s for math (rounding at ~1000mm coords = 0.1mm granularity → false residuals). `verify_assembly` also builds a union-find **contact graph** from pairs already scanned (exact `distToShape` ≤ `_CONTACT_TOLERANCE` = 0.5mm, or common volume > threshold) and reports connected components: the largest is the main assembly, the rest are `islands` (with `gap_mm`/`nearest_main`).
- **Connectivity auto-audit**: after every committed `cad()` mutation, `cad_operation` re-runs the read-only `verify_assembly` audit and appends a "⚠ Connectivity" warning (formatted by `_format_connectivity_warning`) to both the tool response and the recorded step's `result_summary`; `detect_risks` surfaces it as a `disconnected_islands` risk in `session_status`. Guards: skipped when `object_count > _AUTO_AUDIT_MAX_OBJECTS` (300), when the addon is old (no `islands` key), or globally via `--no-auto-audit` (`ServerState.auto_audit`). Audit failures never block the mutation.
- **Assembly mode (`assembly_session` tool)**: independent state machine for mate-based assembly with PERSISTENT joints (FreeCAD 1.1 native Assembly workbench — `Assembly::AssemblyObject` with `Type="Assembly"`, `App::Link` components that own Placements, `JointObject.Joint` joints in the JointGroup). Ops: start(ground) / add_component / mate / solve / unmate / rollback(to_step) / verify / status / complete. Mate refs: `{"part", face|anchor|point}` — resolved to `(link, ["FaceN", "VertexM"])` where the vertex sets the mate landing point (GUI click semantics). `preSolve` (matchJCS) runs before the final single `solve(True)` — skipping it lands mates with faces perpendicular; repeated solve passes corrupt storePrev state. Frames: `link.Shape` is GLOBAL, joint references/`findPlacement` are part-LOCAL; residuals are geometric truth (`fa.distToShape(fb)` + normal angle), never JCS math. `trim={"winner":...}` bakes a non-destructive cut in the loser-link local frame and re-points the link; rollback deletes joints/cuts, re-points links, restores pre-mate placements (precomputed per-step undo, merged by `assembly_state.plan_rollback`). Gap profiles sample the a-face UV grid and measure perpendicular lift from the mate plane (overhang ≠ gap).
- **Modeling sessions**: `session_state.py` binds a session to one document. Every committed mutation runs inside a FreeCAD transaction (addon `_run_op_with_screenshot` wraps `doc.openTransaction`), so `session_rollback` = `doc.undo()` × N + log truncation; removed steps sit in a redo buffer until a new step (mirrors FreeCAD redo semantics). `execute_code` steps are non-atomic and block rollback unless forced. Sessions/patterns persist under `$CADPILOT_HOME` (default `~/.cadpilot/`). Mutation results carry an `objects` fingerprint (sorted object names) used to detect state drift after rollback.
- **Knowledge hierarchy**: prompts instruct ① model's own knowledge → ② `recall_patterns` → ③ `inspect_freecad`; successful approaches are stored via `save_pattern`/`session_complete`.
- **Name sanitization**: FreeCAD sanitizes document/object names (spaces → underscores, deduplication). RPC handlers return the *actual* name from FreeCAD, not the requested name.

## Coding Conventions

- Python 3.12+ (uses `X | None` union syntax, `type` alias).
- Logging: use `logging.getLogger("CADPilotserver")` in both MCP server and addon code.
- Tool operations: each tool has a dedicated `_operation` function in `operations/core.py` that takes a `FreeCADConnection` as its first arg and returns a `ToolResponse` (`list[TextContent | ImageContent]`).
- Addon code uses `FreeCAD.Console.PrintMessage/PrintError/PrintWarning` for FreeCAD's Report View.
- Settings persisted as JSON via `cadpilot_settings.json` (in FreeCAD's user data dir).
- **Docstring budget (prompt economy)**: every `@mcp.tool()` docstring is injected into the AI client's context on `tools/list`. Keep docstrings to a 1-3 line summary + brief Args — long parameter/semantics references go in `src/cadpilot/tool_docs.py` (`CAD_OP_DOCS`) and are served on demand via the `operation_help` tool. `tests/test_operation_help.py::test_tool_docstring_budget` enforces a total budget (< 14,000 chars across all tools); it fails if docstrings creep back up.

## Adding a New MCP Tool

1. Add the operation function in `src/cadpilot/operations/core.py`.
2. Export it from `src/cadpilot/operations/__init__.py`.
3. Add the `@mcp.tool()` handler in `src/cadpilot/server.py` that calls the operation.
4. Add the corresponding RPC method in `addon/CADPilot/rpc_server/rpc_server.py` (on the `FreeCADRPC` class).
5. Add the client proxy method in `src/cadpilot/freecad_client.py`.
6. If the tool touches the document/GUI, dispatch via `dispatch_to_gui()` in the addon.
