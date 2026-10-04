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
  diagnostics.py          # diagnose tool: cross-platform fault probing that works with FreeCAD down (no FreeCAD import)
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
    step_journal.py       # Pure step model: StepRecord, planned/done arithmetic, review helpers
    step_engine.py        # FreeCAD half of the journal — apply_op, per-step transactions, undo
    step_panel.py         # CADPilot Steps dock — review loop UI (status dots, param editor)
    dbglog.py             # Ring-buffer debug log + rotating file (readable while GUI is wedged)
    request_log.py        # Tags log records with the RPC request that caused them

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
uv run cadpilot --screenshot-mode file    # save screenshots to disk, return only paths
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
           "tip_policy",
           "feature_ops",
           "request_log",
           "dbglog",
           "step_journal",
           "step_engine",
           "step_panel",
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

- **GUI thread rule**: All FreeCAD document/GUI operations MUST run on FreeCAD's main (GUI) thread. The addon uses `dispatch_to_gui()` to queue lambdas and a `QTimer` waker to process them. Never call FreeCAD APIs directly from the RPC server thread. User interaction has priority over queued RPC work (`gui_dispatch.py`): on win32 the physical button state (`GetAsyncKeyState`) is authoritative in BOTH directions — Qt's `mouseButtons()` is event-delivered and therefore stale exactly while the event loop is busy, so trusting it alone misses fresh presses and lets tasks start mid-drag (the "frozen while the LLM models" bug); the drain loop also re-checks the mouse/popup/modal guards between tasks so a mid-drain press pauses the backlog, and the phantom caps (static 10s / hold 15s, time-based) exist only for the non-win32 heuristic path — never cap an OS-confirmed real hold.
- **Two-process model**: The MCP server and FreeCAD run in separate processes. They communicate exclusively via XML-RPC on port 9875. The MCP server never imports FreeCAD.
- **MCP version compatibility**: `server.py` imports `FastMCP` from `mcp.server.fastmcp` (1.x) with a fallback to `MCPServer` from `mcp.server.mcpserver` (2.x). Keep both paths working.
- **Timeouts**: Default XML-RPC transport timeout is 150s. `execute_code` has a 90s GUI-thread timeout; use `execute_code_async` for longer operations.
- **Reconnect**: `FreeCADConnection._invoke` rebuilds the XML-RPC proxy and retries once on dead-connection errors (FreeCAD/addon restart). Socket timeouts are NOT retried — the op may still be executing server-side.
- **Screenshot handling**: Screenshots are OPTIONAL and off by default. Per-call `with_screenshot` params opt in; `--with-screenshots` makes them default-on; `--only-text-feedback` is a hard off that overrides everything (see `ServerState.resolve_screenshot`). Screenshots are base64 PNG via temp files. Mutation tools (create/edit/delete/execute_operations) capture the screenshot inline in the same RPC dispatch; the client falls back to a second `get_active_screenshot` call against old addons. When no explicit size is given, the long edge is capped at 512px (`DEFAULT_MAX_DIM` in `view_manager.py`). `--screenshot-mode file` writes screenshots to `$CADPILOT_HOME/screenshots/` (keep last 20) and returns only the file path instead of an inline base64 image; the mode is a module global in `responses.py` set from `main()`. Some view types (TechDraw, Spreadsheet) don't support screenshots — `get_active_screenshot` returns `None` in those cases.
- **Async tasks**: `execute_code_async` returns a `task_id`; status, `task_print()` output, and tracebacks are kept in a bounded in-memory registry (`_async_tasks`, FIFO max 50) and polled via `get_task_result`. `sys.stdout` is never redirected for background tasks (process-wide race).
- **Unified cad() tool**: All mutations go through `cad(operation=...)` (nsforge math()-style dispatcher) to keep the tool list small. Steps: the operation functions in `operations/core.py` remain the implementation; `cad_operation` dispatches and records session steps. Scope is modeling-only — FEM analysis, the parts library, and `reload_document` were removed in v0.2. Feature ops (boolean/fillet/chamfer/loft/sweep/mirror/pattern, plus Sketcher/PartDesign: variables/sketch/pad/pocket/revolution/groove/thickness/draft since v0.3, plus datum_plane/hull since v0.4) share the single RPC `create_feature` and pass params as a spec dict (obj_name = base, obj_properties = params). For `sketch`/`variables`/`datum_plane`/`hull`, obj_name names the NEW object and `spec["base"]` carries it (see `CAD_NO_BASE_OPERATIONS`). Sketches are built atomically in `sketcher_ops.py` (geometry + constraints in one transaction, solver runs immediately); results carry `dof`/`fully_constrained`/`warnings` via `describe_feature`, failures roll back with solver diagnostics. Thickness/draft support both the FreeCAD ≥1.1 LinkSub `Base` layout and the ≤1.0 `Faces` property (probe `PropertiesList`).
- **Sketch mode details** (live-verified on FreeCAD 1.1.3): sketch `external: [[obj, "EdgeN"|"VertexN"], ...]` adds external geometry (GeoIds from -3 in list order, start/end points ONLY — `mid`/`center` on external ids fails at solve with MalformedConstraints, so `_check_external_point_refs` rejects it up front); out-of-body targets are auto-bridged via `PartDesign::SubShapeBinder` (`_external_binder`, idempotent `Ext_<obj>_<elem>`) because PartDesign rejects external geometry outside the sketch's body, and `addExternal` takes `(str, str)`. `datum_plane` attaches a PartDesign::Plane to an origin plane (`body.Origin.OriginFeatures` Role lookup) or an existing face (FlatFace + optional offset); sketches attach via `plane={"datum": name}`. `hull` = visual hull: intersect 2-3 view-profile sketches extruded along their sketch normals (extent = union bbox projected per-normal ±`margin`, default max(1mm, 5% of diagonal)); result is a STATIC `Part::Feature` (no proxy — survives document reload), same-name re-run replaces the Shape in place (iterate), multiple solids → largest wins, empty intersection → RuntimeError. v1 limits: view sketches at the global origin, one closed outer profile per view. **Attachment fusion**: pad/pocket on a sketch attached to a solid's face (directly or through a datum plane) operate on THAT solid via attachment — pocket cuts it, pad fuses into it; do NOT pad-then-boolean-cut (the pad already contains the base solid). **execute_code + openTransaction**: the RPC handler now opens a transaction around every snippet and aborts it if the code raises, so a failing run rolls back cleanly and a leaked transaction can no longer poison later recomputes — user code that prefers to manage its own transaction still works (they merge; see the transactional-`execute_code` bullet).
- **Geometry sensing**: `measure_geometry`/`get_topology`/`check_interference` are read-only Shape queries (addon `geometry_query.py`), dispatched to the GUI thread without a transaction. Values are rounded to 4 significant digits; topology lists are size-sorted and paginated (`limit`/`offset`).
- **Assembly toolchain**: `get_anchors`/`set_anchors`/`assemble`/`verify_assembly` (addon `assembly_ops.py`) give the model data-driven spatial awareness — no screenshots required. Anchors are named points+directions: auto-derived per object (`bbox_center/min/max`, `com`, `axis_mid/start/end` from the dominant cylindrical face, `face0-2_center` from the largest planar faces) plus explicit named ones stored as JSON in an `App::PropertyString` named `MCP_Anchors` in LOCAL coords (they follow Placement). `set_anchors(coord_frame="global")` converts via `obj.Placement.inverse()` at write time — use it whenever the source coordinates are global, because many objects carry non-identity Placements. `assemble` takes a mate list `{obj, anchor, target, target_anchor, mode: center|touch|axis, offset}`, applies each mate in one FreeCAD transaction, then RE-RESOLVES the anchor post-move to report per-mate residuals (mm + deg) and aborts on `tolerance` violation; partial commit via `commit_if` when at least one mate passed. `verify_assembly` audits the whole document: floating objects (nearest-neighbour distance via bbox prefilter + `distToShape`), interferences (common volume for bbox-overlapping pairs), and explicit anchor-pair checks with per-check tolerance. Hidden objects (`ViewObject.Visibility = False` — boolean bases, tool compounds, work geometry) are excluded from the audit and counted in `summary.skipped_hidden`. PRECISION RULE: `_r()`/`_vec()` rounding (4 sig digits) applies ONLY at the report boundary — `_resolve_anchor`/`_auto_anchor_map` must return RAW `FreeCAD.Vector`s for math (rounding at ~1000mm coords = 0.1mm granularity → false residuals). `verify_assembly` also builds a union-find **contact graph** from pairs already scanned (exact `distToShape` ≤ `_CONTACT_TOLERANCE` = 0.5mm, or common volume > threshold) and reports connected components: the largest is the main assembly, the rest are `islands` (with `gap_mm`/`nearest_main`).
- **Connectivity auto-audit**: after every committed `cad()` mutation, `cad_operation` re-runs the read-only `verify_assembly` audit and appends a "⚠ Connectivity" warning (formatted by `_format_connectivity_warning`) to both the tool response and the recorded step's `result_summary`; `detect_risks` surfaces it as a `disconnected_islands` risk in `session_status`. Guards: skipped when `object_count > _AUTO_AUDIT_MAX_OBJECTS` (300), when the addon is old (no `islands` key), or globally via `--no-auto-audit` (`ServerState.auto_audit`). Audit failures never block the mutation.
- **Assembly mode (`assembly_session` tool)**: independent state machine for mate-based assembly with PERSISTENT joints (FreeCAD 1.1 native Assembly workbench — `Assembly::AssemblyObject` with `Type="Assembly"`, `App::Link` components that own Placements, `JointObject.Joint` joints in the JointGroup). Ops: start(ground) / add_component / mate / solve / unmate / rollback(to_step) / verify / status / complete. Mate refs: `{"part", face|anchor|point}` — resolved to `(link, ["FaceN", "VertexM"])` where the vertex sets the mate landing point (GUI click semantics); mate results report `landing` (the actual face/vertex per side) and `warnings` when that vertex sits far from the ref's intent point (face center / anchor position / the point itself — nearest-vertex choice is arbitrary on symmetric faces). `preSolve` (matchJCS) runs before the final single `solve(True)` — skipping it lands mates with faces perpendicular; repeated solve passes corrupt storePrev state. Frames: `link.Shape` is GLOBAL, joint references/`findPlacement` are part-LOCAL; residuals are geometric truth (`fa.distToShape(fb)` + normal angle), never JCS math. `trim={"winner":...}` bakes a non-destructive cut in the loser-link local frame and re-points the link; rollback deletes joints/cuts, re-points links, restores pre-mate placements (precomputed per-step undo, merged by `assembly_state.plan_rollback`). Gap profiles sample the a-face UV grid and measure perpendicular lift from the mate plane (overhang ≠ gap).
- **Modeling sessions**: `session_state.py` binds a session to one document. Every committed mutation runs inside a FreeCAD transaction (addon `_run_op_with_screenshot` wraps `doc.openTransaction`), so `session_rollback` = `doc.undo()` × N + log truncation; removed steps sit in a redo buffer until a new step (mirrors FreeCAD redo semantics). `execute_code` is transactional too: the addon wraps the snippet in a transaction and reports `changed` (whether it produced an undo entry), so a mutating run is an ATOMIC session step and rollback undoes it like any cad() call; a read-only run is **not recorded at all** — it owns no transaction, and a step that cannot be undone would break the log's one-transaction-per-step invariant (which `session_rollback`'s `n = step_count - to_step` relies on); likewise a run that changed a DIFFERENT document than the session's is not recorded — the addon reports `document` alongside `changed`, and recording a foreign-document step would make `session_rollback` pop the wrong document's undo stack (direct it to `step_control` on that document instead). Sessions/patterns persist under `$CADPILOT_HOME` (default `~/.cadpilot/`). Mutation results carry an `objects` fingerprint (sorted object names) used to detect state drift after rollback.
- **Transactional `execute_code`**: the RPC handler wraps the snippet in a FreeCAD transaction so its document changes become ONE undo entry — a bare property write is otherwise not undoable at all, which is why an LLM that models exclusively through `execute_code` (very common) produced a journal nothing could roll back. Mechanics (`rpc_server.execute_code`): wrap only when `doc.HasPendingTransaction` is false (transactions do NOT nest — a snippet that opens its own merges into ours, and one that aborts rolls ours back too); an EMPTY commit adds no undo entry, so a read-only run costs nothing; `changed = doc.UndoCount > undo_before` after the commit is the signal (commit/abort return `None`, so the undo count is the only reliable probe); a snippet that raises aborts our transaction, so a failing `execute_code` no longer leaves half-applied mutations. The snippet runs through `rpc_server.exec_snippet`, shared with journal replay so a re-run sees the same namespace (`FreeCAD`, `FreeCADGui`, `Part`, …). Live-verified on FreeCAD 1.1.4: mutating snippet → atomic+executable journal step (code in params) → `rollback_to 0` with NO force → `replay` re-ran it and skipped the read-only step → exact geometry restored.
- **Step journal (steps panel + review loop)**: a *second*, FreeCAD-side log, distinct from the MCP session above — it must survive with the RPC server stopped, so it lives on the document as the `App::PropertyString` `MCP_StepJournal` (JSON: `{version, meta, records}`). `step_journal.py` is the pure model (no FreeCAD import, unit-tested from `tests/`): `StepRecord` + planned/done/failed arithmetic + the review helpers `plan_reject`/`set_accepted`/`update_planned`/`insert_steps`/`invalidates_plan`. `step_engine.py` is the FreeCAD half (GUI thread): `apply_op` is the single entry point shared by the RPC handler `journal_op` and the panel, and every mutating result carries a compact `journal` snapshot (counts, drift, per-step state) so clients need no follow-up `status` call. `step_control` verbs: run_next/run_all/run_to, rollback_to (records return to `planned`), reexecute (params merge), accept/unaccept (soft lock), reject (undo the step and DROP it and everything after — uniform with "a new commit invalidates the planned tail"), update (edit a planned/failed step in place), insert (into the planned tail only — the tail is insert/append-only, never edit history), replay (rollback to 0 + re-run: rebuild the model from the journal), snapshot, clear_plan, reset. `batch` steps are executable, and so are mutating `execute_code` steps (the snippet is stored in `params["code"]` and re-run through `rpc_server.exec_snippet`, so replay rebuilds an execute_code-built model); a read-only `execute_code` stays non-executable and is skipped. Journal writes happen INSIDE the step's transaction, but FreeCAD's undo does NOT restore document-level properties, so rollback/reexecute reconcile the log explicitly (`sj.rewind`) — that reconciliation is the primary mechanism, and `drift` (anchored on `sj.last_atomic_done`, the last transaction-bearing step) flags a manual Ctrl+Z. `plan_rollback`/`plan_reject` count only TRANSACTION-BEARING records for `undo_count` — a non-atomic record owns no undo entry, and counting it would eat an earlier step's transaction off the plain stack. `_rollback` then VERIFIES what undo actually did instead of trusting the count (the stack is shared with the GUI; an interleaved manual transaction pops under the rollback's name while the count still matches): objects the post-target steps created must be gone and journal-built objects expected at the target must be present. Removal/verification sets come from `sj.created_since` — per-record `objects_before`/`objects_after` DIFFS, never whole-snapshot subtraction (every `objects_after` lists the entire document, so at index 0 the target snapshot is empty and whole-list subtraction deletes objects that predate the journal — the user's own work; live-caught data loss, fixed in v0.5.4). On short count, stranded steps (`sj.steps_without_undo`), leftover or missing objects it escalates: `restored: rebuild` (remove `created_since(records, 0)`, reset done AND failed records to planned — a failed step's transaction aborted, so re-running is safe, and skipping it would run a later step against a missing dependency — then re-run 1..to_index; a failed re-run returns `success: false` honestly), or `restored: partial` when `unrecoverable_steps` (mutated, not executable, at/before the target) blocks a rebuild — remove the doomed steps' objects and warn that their property changes cannot be restored. A clean undo reports `restored: native`. `reject` removes `created_since(index-1)` unconditionally (its records are being DROPPED, so no rebuild exists) and warns about leftovers; `reexecute` refuses when the undo came up short or left later steps' objects behind, pointing at `rollback_to`. The panel's `_apply` prints result `warnings` (red) after a success, so a rebuild/partial degradation is visible in the dock, not only in the MCP reply. `blocking` (force required) is `not atomic AND mutated`: a record that provably changed nothing (a read-only `execute_code`, `mutated=False`) cannot make undo revert the wrong change, so it must NOT stand in rollback's way — otherwise trailing inspections made every rollback demand `force`. Accepted steps are a soft lock: `rollback_to`/`reexecute`/`replay` refuse to cross one without `force=true`; `reject` does not (it is the deliberate act of destruction). `reject`/`rollback` across a mutating non-atomic record needs force. An `execute_code` commit only discards the planned tail when it actually changed the object set (`sj.invalidates_plan`) — a read-only inspection must not delete the user's plan. `snapshot` appends a done+accepted, non-atomic, non-executable marker (params.note, label = note or "manual baseline") for the "user modeled off-journal, LLM continues from here" flow: the accepted soft-lock transitively protects the manual work from blind rollback, `objects_before` vs `objects_after` names what the journal missed (`added` in the result), and the planned tail is KEPT (a snapshot is a marker, not a commit). `params.accept_done` accepts every done step in the same call (bundling "everything so far is correct" into the baseline instead of ten accept clicks), and its `objects_before` anchors on the LAST DONE record's snapshot — the physical last record may be a planned tail, which never ran and carries no snapshot, and anchoring there would mark the user's whole document as "new". The dock (`step_panel.py`) drives `step_engine` directly (works with the RPC server down): status-dot list, progress bar, inline JSON parameter editor, a persistent timestamped log console (every action result, successes dim/plain and failures red — the single place outcomes land, never duplicated in the footer), context menu, and per-state action enablement. Steps/Details/Log are QSplitter regions the user can re-partition freely (sizes persist in `cadpilot_settings.json` as `step_panel_splitter`, debounced on drag). Run-op failures name the failing step (`step 3 (boolean): ValueError: ...`) in the result error, so `get_addon_log` stays LLM-debuggable; the panel also mirrors journal failures caused outside itself (MCP step_control) into its log, deduplicated against its own action narration via `_in_apply`. **Manual-edit sync**: `step_engine`'s document observer (installed at import, reload-safe singleton) mirrors GUI edits on objects a done step produced back into that step's `params["obj_properties"]` — human corrections survive reexecute/replay (object -> journal; the reverse is reexecute). Scope: for `create_object`/`edit_object`, scalar keys already present in the params (the spec's structure is never invented) PLUS `Placement` — always tracked, because a reexecute re-applies obj_properties and would otherwise teleport the part to the origin; Placement is written in the property_mapper convention (Angle in DEGREES — `Rotation.Angle` reads back radians, so `_placement_json` converts, or a round-trip turns 90° into 1.57°), and is NOT claimed when a `move` step targets the object (a move is relative; an absolute create-time Placement plus the move would double-apply). For feature ops a fixed per-op map (`_FEATURE_SYNC`: pad/pocket Length→length, revolution/groove/draft Angle→angle, thickness Value→value, fillet/chamfer Radius/Size→radius/size) applies, filtered to keys present in the params. FreeCAD ≥1.1 keeps fillet/chamfer sizes in per-edge `Edges` tuples instead of a scalar property, so `Edges` is claimed too and syncs only when all tuples share one size (`_uniform_edge_size`) — per-edge sizes have no spec representation. When several done steps touch one object, the LATER step owns each property. Syncs are logged to the addon log (`journal sync: step N obj.prop = v (manual edit)`) and surfaced in the panel log via `pop_sync_events`. The observer must ALSO ignore engine-driven property writes: undo/redo/re-runs restore OLD values, and mirroring those back is how a reexecute silently reverted a manual correction (live-verified: Height synced to 10, reexecute undid the edit's transaction, the observer "manual-edited" the journal back to 6, the re-run rebuilt at 6). `_EngineQuiet` (module flag `_ENGINE_ACTIVE`) mutes `slotChangedObject` across `_stack_op` (undo/redo) and `run_record`'s execute/abort/final-recompute windows; a genuine GUI edit never happens inside one. The aborts inside `rpc_server.py` are deliberately NOT muted — they restore values the params already describe, so the diff check swallows them (and a failed snippet's sync rolling back with its transaction is correct). It is deliberately glyph-free: actions are text-only (the platform's standard icons clash with FreeCAD's chrome) and step states are runtime-painted dots. The styling is MATLAB-inspired — flat sections, hairline separators, one accent — from two hand-tuned palettes (dark/light), chosen by sampling a rendered main-window pixel, because FreeCAD themes are app-level stylesheets that leave the QPalette light even in dark mode (never trust `palette()` for theme detection). The dock is looked up by `objectName` with duplicate collapse, because `findChild(PanelClass, …)` matches on the class object and misses docks built before a hot reload (see `tests/test_addon_gui_wiring.py`).
- **Addon diagnostics (`get_addon_log`, `diagnose`)**: `dbglog.py` ring buffer + rotating file under FreeCAD's user data dir; the RPC handler is deliberately NOT GUI-dispatched, so the log stays readable exactly when the GUI thread is wedged and every other call hangs. `request_log.py` tags records with the RPC (`req#N`) that caused them, including work carried onto the GUI thread. Its MCP-side counterpart is the `diagnose` tool (`src/cadpilot/diagnostics.py`, pure stdlib, never imports FreeCAD): it still answers when FreeCAD is down or frozen, probing the RPC endpoint (ping on a 5s timeout + a raw TCP connect), the FreeCAD process, port listeners, the installed addon (symlink/copy, complete or not) across every FreeCAD user-data dir (`v1-1`, `v0-21`, … plus the unversioned legacy layout), `initgui_debug.log`, the addon log's freshness and `cadpilot_settings.json`. Two things it must get right: (a) a *listening* port that does not answer ping is a wedged GUI thread, not a setup problem — the opposite advice; (b) `initgui_debug.log` also receives routine initgui lines, so only files actually containing a crash marker are reported as a crash (and a crash found while the RPC is reachable is labelled "earlier start"). FreeCAD install dirs come from the `.FCStd` file association on Windows (a normal install is not on PATH) and standard paths elsewhere; local file checks are skipped entirely for a remote `--host`. `tests/test_diagnostics.py` pins the layout helpers, a live loopback XML-RPC endpoint, a closed port and the verdict branches. Subprocess probes capture bytes and decode with `errors="replace"` — `text=True` uses the locale codec and a localized `tasklist`/`netstat` kills the pipe reader.
- **InitGui.py bare-exec trap**: FreeCAD runs `InitGui.py` with a bare `exec(code)` into a namespace that is NOT the `__globals__` of the functions the file defines, so a module-level name is invisible inside `_bootstrap()`'s nested helpers (only their enclosing function's closures and builtins resolve). Getting this wrong is silent and total: `find_addon_dir()` raised `NameError: name 'contextlib' is not defined`, the bootstrap died, and the addon never loaded — no workbench, no RPC server, stale addon log (live on 1.1.4). Everything the nested helpers need must be imported *inside* `_bootstrap()`; the last-resort crash handler writes `initgui_debug.log` to the addon dir first, then next to the FreeCAD executable when `__file__` is unavailable. `tests/test_addon_gui_wiring.py::test_bootstrap_helpers_never_read_module_level_names` enforces the rule with an AST scope walk (it flags the pre-fix code as `find_addon_dir(): contextlib`).
- **PartDesign stress-test findings (v0.5.3)**: three silent-wrong-geometry bugs a parametric flange (variables → constrained sketch → pad → pocket → 6-hole bolt ring) exposed. (a) `pattern` used Draft's array, which replicates the base object's whole **Shape** — for a PartDesign feature that Shape is the entire body, so "pattern this hole" produced N overlapping copies of the whole part; it now builds a `PartDesign::PolarPattern`/`LinearPattern` inside the feature's Body with `Originals=[feature]` (`_build_pd_pattern`, body found via `InList`, axis datum via `Origin` `Role` lookup). FreeCAD 1.1 cannot be driven into doing this reliably through Python (occurrences can come out coincident — verified against every axis/spacing form), so when the result volume equals the base's the op **raises** rather than returning one hole where six were asked for; Part-level bases still use the Draft path. (b) `plane={"face": [obj, "FaceN"]}` is unstable — face names are re-derived after every feature, so a name read off one feature can mean a different face on the next, attaching the sketch to a side and cutting air while reporting success; `_resolve_semantic_face` (`sketcher_ops.py`) accepts `"+Z"`/`"top"`/`-X`/… and picks the planar face whose normal matches, breaking ties by extremity along that axis — **prefer a direction over a name**. (c) a pocket/groove that removed nothing is now reported: `describe_feature` compares against the feature's predecessor (`BaseFeature`, NOT the sketch's attachment support — for a body the tip is the real baseline) and returns a `warnings` entry. Also: `_run_steps`/`reexecute` params merge at the TOP level, so a step's operation parameters must be edited under `obj_properties` (a `variables` change is `{"obj_properties": {"cells": …}}`); and `ensure_panel()` rebuilds a dock left over from an older class, so panel changes land on hot reload without restarting FreeCAD. The panel's detail pane shows an execute_code step's **snippet** (always kept in `params`), editable — edit it and press Re-run to iterate — and rows carry an effect label (`+6 object(s): Hole1, …` / `read-only`) instead of the code's boilerplate import line.
- **Batch step re-execution**: `cad(operation="batch")` sub-ops use the RPC schema's `{"action": ...}` key and are journaled verbatim, while journal-native steps use `{"operation": ...}`. BOTH sides accept both keys — `sj.sub_operation()` is the single reader on the re-execution side (so journals written by an older addon are repaired rather than rejected), and `_run_one_operation` tolerates `operation` on the initial run (else a journal-native batch replays fine but dies "unknown action: None" the first time). The old failure ("operation '' is not re-executable") undid the step and then failed to re-run it, leaving the model stuck one transaction behind, which is what "roll back cannot go back" looked like. `run_next`/`run_all`/`run_to`/`replay` skip non-executable records (execute_code inspections are normal journal citizens): the record is marked done with result `skipped: not re-executable` and reported in the result's `skipped` list — a hard failure here made replay unusable after any inspection.
- **Knowledge hierarchy**: prompts instruct ① model's own knowledge → ② `recall_patterns` → ③ `inspect_freecad`; successful approaches are stored via `save_pattern`/`session_complete`.
- **Name sanitization**: FreeCAD sanitizes document/object names (spaces → underscores, deduplication). RPC handlers return the *actual* name from FreeCAD, not the requested name.

## Coding Conventions

- Python 3.12+ (uses `X | None` union syntax, `type` alias).
- Logging: use `logging.getLogger("CADPilotserver")` in both MCP server and addon code.
- Tool operations: each tool has a dedicated `_operation` function in `operations/core.py` that takes a `FreeCADConnection` as its first arg and returns a `ToolResponse` (`list[TextContent | ImageContent]`).
- Addon code uses `FreeCAD.Console.PrintMessage/PrintError/PrintWarning` for FreeCAD's Report View.
- Settings persisted as JSON via `cadpilot_settings.json` (in FreeCAD's user data dir).
- **Docstring budget (prompt economy)**: every `@mcp.tool()` docstring is injected into the AI client's context on `tools/list`. Keep docstrings to a 1-3 line summary + brief Args (drop zero-information lines like "doc_name: Document name."; inline short Returns) — long parameter/semantics references go in `src/cadpilot/tool_docs.py` (`CAD_OP_DOCS`) and are served on demand via the `operation_help` tool. `tests/test_operation_help.py::test_tool_docstring_budget` enforces a total budget (< 11,000 chars across all tools); it fails if docstrings creep back up.

## Adding a New MCP Tool

1. Add the operation function in `src/cadpilot/operations/core.py`.
2. Export it from `src/cadpilot/operations/__init__.py`.
3. Add the `@mcp.tool()` handler in `src/cadpilot/server.py` that calls the operation.
4. Add the corresponding RPC method in `addon/CADPilot/rpc_server/rpc_server.py` (on the `FreeCADRPC` class).
5. Add the client proxy method in `src/cadpilot/freecad_client.py`.
6. If the tool touches the document/GUI, dispatch via `dispatch_to_gui()` in the addon.
