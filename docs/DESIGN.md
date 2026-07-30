# CADPilot — MCP Design Document

**English** | [Chinese](DESIGN.zh-CN.md)

This document describes the architecture and key design decisions of CADPilot, an MCP (Model Context Protocol) server that lets AI clients drive FreeCAD.

## 1. Goals

* Give AI clients **complete** control of FreeCAD: documents, parametric modeling, constrained sketches, assembly, and quantitative verification.
* Keep the AI client's **context budget low**: small tool list, short docstrings, text-first responses, on-demand documentation.
* Make every mutation **transactional and rollback-able**, so an AI agent can experiment and backtrack like a human designer.
* Provide **data-driven spatial awareness** (anchors, topology, measurements) so positioning does not depend on screenshots or guesswork.

## 2. Two-Process Architecture

```
AI client (Claude Code, Cherry Studio, …)
   │  MCP over stdio
   ▼
cadpilot MCP server (src/cadpilot, Python ≥ 3.12, runs via uvx)
   │  XML-RPC over TCP, localhost:9875
   ▼
FreeCAD addon (addon/CADPilot, hosted inside the FreeCAD process)
   │
   ▼
FreeCAD document / GUI
```

Why two processes?

* **Process isolation**: the MCP server never imports FreeCAD. It can run anywhere (including on another machine via `--host`) and survives FreeCAD restarts — the XML-RPC client detects a dead connection, rebuilds the proxy, and retries once.
* **MCP lifecycle**: AI clients launch and kill stdio servers at will. FreeCAD is a long-lived GUI application; the addon keeps the CAD state while MCP servers come and go.
* **Version compatibility**: the MCP server imports `FastMCP` from `mcp.server.fastmcp` (MCP 1.x) with a fallback to `MCPServer` (MCP 2.x).

The default transport timeout is 150 s. Socket timeouts are *not* retried — the operation may still be executing server-side.

## 3. GUI-Thread Dispatch

FreeCAD's document tree and GUI APIs are **not thread-safe**; all document/GUI work must run on FreeCAD's main (GUI) thread. The XML-RPC server runs on its own thread, so the addon ferries work across:

* `dispatch_to_gui(task)` enqueues a wrapped callable on a shared queue and blocks the RPC thread (default 60 s timeout) until the GUI thread reports the result through a **per-call response queue** — a timeout in one call can never corrupt another call's response.
* The GUI thread is woken **immediately** via a Qt signal (`QueuedConnection`), with a 500 ms heartbeat timer as fallback.
* The drain loop skips ticks while mouse buttons are held, or a popup/modal is open, so MCP tasks cannot interrupt 3D navigation or dialogs. A re-entrancy guard prevents nested drains from `processEvents` inside a task.
* Exceptions inside a task are caught, logged to FreeCAD's Report View, and returned as error strings — they never kill the dispatch loop.
* On stop, a sentinel suppresses the timer reschedule so the loop actually terminates.

`execute_code` user scripts run in a **copy** of the addon module's namespace, so assignments in user code cannot leak into and corrupt the RPC server's own globals.

## 4. Tool Surface Design

### 4.1 Unified `cad()` dispatcher

Instead of one MCP tool per operation, all mutations go through a single `cad(operation=...)` tool (in the style of nsforge's `math()`):

* `create_object` / `edit_object` / `delete_object` / `batch`
* Part features: `boolean` (multi-tool list support), `fillet`, `chamfer`, `loft`, `sweep`, `mirror`, `pattern`, `move`
* Sketcher/PartDesign: `variables`, `sketch`, `pad`, `pocket`, `revolution`, `groove`, `thickness`, `draft`, `datum_plane`, `hull`

This keeps the resident tool list small — important because every tool definition is injected into the AI client's context on `tools/list`.

### 4.2 Docstring budget

Every `@mcp.tool()` docstring costs context tokens in *every* conversation. Docstrings are therefore limited to a 1–3 line summary plus brief args; long parameter/semantics references live in `tool_docs.py` and are served on demand via the `operation_help` tool. A test enforces a total docstring budget (< 14,000 chars).

### 4.3 Knowledge hierarchy

Prompts instruct the model to consult, in order: ① its own knowledge → ② `recall_patterns` (a persistent pattern store of successful workflows) → ③ `inspect_freecad` (runtime introspection of object properties/methods or API docstrings). Successful approaches are stored back via `save_pattern` / `session_complete`, so the system accumulates project-specific knowledge.

## 5. Transactions, Sessions, and Rollback

Every committed mutation runs inside a **FreeCAD transaction** (`doc.openTransaction`). On top of that, modeling sessions (`session_start` … `session_complete`) record each mutation as a step with:

* operation, description, parameters, result summary
* an `objects` fingerprint (sorted object names) for drift detection
* notes (observations/assumptions) attached by the model or user

`session_rollback(to_step)` is then just `doc.undo()` × N plus log truncation — no delete-and-rebuild. Removed steps sit in a redo buffer until a new step clears it, mirroring FreeCAD's redo semantics. `execute_code` steps are non-atomic and block rollback unless forced.

Mutation responses are auto-audited: a read-only connectivity check (`verify_assembly`) re-runs after every committed `cad()` call and appends warnings about disconnected islands; `session_status` surfaces risks (state drift, non-atomic steps, closed documents) and next-step suggestions. The audit never blocks the mutation itself.

Sessions and patterns persist as JSON under `$CADPILOT_HOME` (default `~/.cadpilot/`), written atomically (tmp + rename).

## 6. Sketcher and PartDesign Ops

Sketches are built **atomically** in one transaction: geometry (list order = GeoId) plus constraints, solved immediately. Results report `dof` / `fully_constrained` / solver warnings; conflicting or malformed specs roll back with solver diagnostics (`ConflictingConstraints`, `RedundantConstraints`).

Notable details:

* `external: [[obj, "EdgeN"|"VertexN"], …]` adds external geometry. Only start/end points may be referenced (`mid`/`center` fail at solve time and are rejected up front). Out-of-body targets are auto-bridged via an idempotent `PartDesign::SubShapeBinder`, because PartDesign rejects external geometry outside the sketch's body.
* `datum_plane` attaches to an origin plane or an existing face; sketches attach via `plane={"datum": name}`.
* **Attachment fusion**: pad/pocket on a sketch attached to a solid's face operate on *that* solid — pocket cuts it, pad fuses into it. Pad-then-boolean-cut is wrong here.
* `hull` builds a visual hull: intersect 2–3 view-profile sketches extruded along their normals. The result is a static `Part::Feature` (survives reload); re-running with the same name replaces the Shape in place for iteration.
* String values starting with `=` in any property are bound via the ExpressionEngine, enabling Spreadsheet-driven parametrics (`variables` op) throughout the chain.

## 7. Geometry Sensing and Feedback

`measure_geometry` / `get_topology` / `check_interference` / `get_positioning_info` are read-only Shape queries dispatched to the GUI thread without a transaction:

* All coordinates are **global** — FreeCAD Shapes carry the object's Placement as their internal location, so no manual transform is applied (a previous double-transform bug was fixed in v0.2).
* Topology lists are size-sorted and paginated (`limit`/`offset`); faces and edges carry semantic info (type, center, normal, radius, axis, start/end points) so the model can pick selectors for follow-up feature ops.
* Rounding (4 significant digits) applies only at the report boundary; internal math uses raw vectors.

## 8. Spatial Positioning and Assembly

The hardest problem in AI-driven CAD is positioning parts relative to each other. CADPilot attacks it with data, not screenshots:

* **Anchors** (`get_anchors` / `set_anchors`): named points + directions per object. Auto-derived (`bbox_center/min/max`, `com`, `axis_*` from the dominant cylindrical face, `face*_center` from the largest planar faces) plus explicit named anchors stored as JSON in an `App::PropertyString` in **local** coordinates so they follow Placement.
* **`assemble`**: a mate list `{obj, anchor, target, target_anchor, mode: center|touch|axis, offset}` applied in one transaction; each anchor is re-resolved post-move to report per-mate residuals (mm + degrees) and abort on tolerance violation.
* **`align_shapes`**: single-step face/edge/vertex alignment (`touch` / `center` / `axis`).
* **`verify_assembly`**: whole-document audit — floating objects (nearest-neighbour distance), interferences (common volume), explicit anchor-pair checks, and a union-find **contact graph** that reports islands relative to the main assembly.

### Assembly sessions (persistent joints)

`assembly_session` is an independent state machine on FreeCAD 1.1's native Assembly workbench: `start` (ground part) → `add_component` → `mate` → `solve` → `verify` → `complete`, plus `unmate` / `rollback(to_step)` / `status`. Components are `App::Link`s that own the Placement; joints persist in the document, so moving a parent part and re-solving repositions children.

Key implementation rules (verified live on FreeCAD 1.1.3):

* `preSolve` (matchJCS) must run before the final single `solve(True)`; repeated solve passes corrupt solver state.
* `link.Shape` is global, but joint references and `findPlacement` are part-local — residuals are measured geometrically (`distToShape` + normal angle), never via joint-coordinate-system math.
* Mate references accept `face` / `anchor` / `point`; the nearest vertex on the face sets *where* the mate lands (GUI click semantics).
* `trim={"winner": "inserted"|"base"}` performs declarative priority trimming: a non-destructive cut baked in the loser link's local frame, with the link re-pointed. Every step precomputes its undo payload, so rollback is a single merged RPC spec executed atomically.
* The MCP side validates the addon result (`{"success": false}`) before recording anything, so a failed RPC never leaves half-mutated session state.

## 9. Screenshots and Token Economy

Screenshots are **optional and off by default**:

* Per-call `with_screenshot` opts in; `--with-screenshots` makes them default-on; `--only-text-feedback` is a hard off that overrides everything (for text-only models).
* Mutation tools capture the screenshot inline in the same RPC dispatch (single round trip); the client falls back to a second call against older addons.
* Default capture is capped at 768 px on the long edge to save tokens; `get_view` accepts explicit sizes.
* Some view types (TechDraw, Spreadsheet) cannot be captured — the addon returns `None`.

## 10. Background Tasks

`execute_code_async` runs background-safe code (long OCCT computations) in a daemon thread and returns a `task_id`; status, `task_print()` output, and tracebacks are kept in a bounded in-memory registry (FIFO, max 50) polled via `get_task_result`. `sys.stdout` is never redirected for background tasks (process-wide race); background code reports via `task_print()` or the thread-safe `FreeCAD.Console`. Background code must not touch the document/GUI — that is what `execute_code` is for.

## 11. Security

* The RPC server binds to `localhost` by default; remote access is an explicit opt-in (**Remote Connections** toggle) that binds `0.0.0.0`.
* When remote is enabled, a filtered XML-RPC server enforces an IP/CIDR **allowlist** (default `127.0.0.1`); invalid entries are rejected at configuration time.
* `execute_code` is arbitrary code execution *by design* — it is the escape hatch that makes the system complete. The threat model therefore treats any reachable MCP client as fully trusted, which is why remote access defaults off and is allowlist-guarded.
* The `--host` value is validated at startup (IPv4/IPv6/hostname).

## 12. Reliability and Compatibility

* **Reconnect**: the client rebuilds the XML-RPC proxy and retries once on dead-connection errors (FreeCAD/addon restart).
* **Graceful degradation**: new clients fall back against older addons (single-shot screenshot RPC, optional params); older clients keep working against the new addon.
* **Name sanitization**: FreeCAD sanitizes document/object names (spaces → underscores, deduplication). RPC handlers always return the *actual* name, not the requested one.
* **Version tolerance**: thickness/draft support both the FreeCAD ≥ 1.1 LinkSub `Base` layout and the ≤ 1.0 `Faces` property (probed at runtime); `Part::Mirroring` vs `Part::Mirror` is resolved by probing.
* **Hot reload**: the addon can be reloaded without restarting FreeCAD (stop RPC server → deferred `importlib.reload` of all submodules → start), with a documented repair path if the restart races the shutdown drain.

## 13. Testing Strategy

The pytest suite (~200 tests) covers the MCP-server side against a fake XML-RPC connection that records every call: response shaping, screenshot policy precedence, reconnect behavior, session/pattern state machines, `cad()` dispatch, assembly state machine (including RPC failure paths), guidance heuristics, and the docstring budget. The addon side requires a live FreeCAD and is verified with `tests/live_sketch_verify.py`, an end-to-end script that builds a parametric bracket, checks expression propagation, failure diagnostics, and undo.
