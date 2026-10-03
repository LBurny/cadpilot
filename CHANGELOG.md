# Changelog

## v0.5.1 (2026-10-04)

### Fixed

- **Batch sub-ops keyed `operation` now run on initial execution, not just on
  replay** (`rpc_server.py`): `_run_one_operation` read only the RPC schema's
  `action` key, so a batch written in the journal-native `operation` style
  failed every sub-op with the cryptic `unknown action: None` while the same
  journal replayed fine. Both sides of the journal boundary now accept both
  keys, and a sub-op missing both gets an explicit "no 'action' (or
  'operation') key" error.
- **`run_all`/`replay` no longer die on non-executable journal records**
  (`step_engine.py`): an `execute_code` inspection is a normal journal citizen,
  but re-running the plan stopped hard at it ("operation 'execute_code' is not
  re-executable"), making replay unusable after any inspection. Such records
  are now marked done and skipped, reported in the result's `skipped` list.
- AST regression tests pin both contracts (`tests/test_step_journal.py`,
  `tests/test_step_engine_ops.py`); the AGENTS.md hot-reload module list now
  includes `step_journal`/`step_engine`.

## v0.5.0 (2026-10-04)

### New features

- **Step journal + steps panel** (`step_journal.py` / `step_engine.py` /
  `step_panel.py`): a FreeCAD-side review loop that survives with the RPC
  server stopped, stored on the document itself (`MCP_StepJournal`). The
  `step_control` tool and the dock share one engine: run_next/run_all/run_to,
  rollback_to, reexecute, accept/unaccept (soft lock), reject (undo and drop
  the step plus everything after it), update, insert, replay (rebuild the
  model from the journal), snapshot and reset.
- **`snapshot` verb**: marks the current state as a done+accepted baseline for
  the "the user modeled off-journal, let the LLM continue from here" flow —
  the accepted soft lock protects manual work from a blind rollback, and the
  record names what the journal missed.
- **Manual-edit sync**: GUI edits to objects a step produced are mirrored back
  into that step's parameters, so human corrections survive
  reexecute/replay — including `Placement` (so a re-run does not teleport the
  part to the origin) and, on FreeCAD ≥1.1, per-edge fillet/chamfer sizes.
- **`diagnose` tool**: cross-platform fault diagnosis that runs on the MCP
  side, so it still answers when FreeCAD is down or frozen. It probes the RPC
  endpoint, the FreeCAD process, port listeners, the addon install, the
  bootstrap crash log, the addon log's freshness and the settings, and ends
  with a verdict plus the generic 5-step order to work through.
- **`get_addon_log` tool**: reads the addon's ring-buffer/rotating debug log
  (per-RPC request ids, GUI-dispatch and transaction traces) — readable even
  when FreeCAD's GUI thread is wedged.

### Fixed

- **The addon could fail to load entirely** (`InitGui.py`): FreeCAD runs it
  with a bare `exec()` into a namespace that is not the `__globals__` of the
  functions the file defines, so the module-level `import contextlib` was
  invisible inside a nested helper. `find_addon_dir()` raised
  `NameError: name 'contextlib' is not defined`, the bootstrap died, and the
  addon silently never loaded — no workbench, no RPC server, stale log. Every
  name the nested helpers need is now imported inside `_bootstrap()`, the
  crash log falls back to the addon dir before the executable's directory, and
  a test enforces the rule with an AST scope walk.
- **Batch steps could not be re-executed** (`step_journal.py`): `cad(batch)`
  sub-ops are journaled with the RPC schema's `action` key, but re-execution
  read `operation`, so `reexecute`/`replay` undid the step and then failed
  with "operation '' is not re-executable" — leaving the model one
  transaction behind. Both keys are now accepted, on the execution side, so
  journals written by an older addon are repaired rather than rejected.
- **Step rollback ate the wrong transaction** (`step_journal.py`):
  `plan_rollback`/`plan_reject` counted non-atomic records (e.g. an
  `execute_code` inspection) toward `undo_count`, so one undo too many was
  issued and an earlier step's object disappeared. Only transaction-bearing
  records count now.
- **Phantom mouse-button state no longer starves the GUI task queue**
  (`gui_dispatch.py`): the drag guard in `process_gui_tasks` deferred all
  queued tasks whenever Qt reported a pressed mouse button. After a
  background launch (or an RDP session) Qt can report `LeftButton` with no
  real interaction, and the state cannot be cleared programmatically —
  `ping` kept answering while every GUI-dispatched call (e.g.
  `execute_code`) timed out, looking like a dropped connection. The guard
  now only defers when the main window is actually active.

### Changed

- **Tool docstrings slimmed** (13.9k → 10.6k chars across all tools): the
  per-tool descriptions the client pays for on every `tools/list` keep only a
  summary and brief Args; the full references live in `tool_docs.py` and are
  served on demand by `operation_help`. The budget test now enforces
  < 11,000 chars.

## v0.4.0 (2026-07-30)

### Renamed to CADPilot

The project has been renamed from `freecad-mcp` to **CADPilot** (AI pilots FreeCAD). All identifiers updated:

- PyPI package: `freecad-mcp` → `cadpilot`
- Python module: `freecad_mcp` → `cadpilot`
- CLI command: `uvx freecad-mcp` → `uvx cadpilot`
- FreeCAD addon directory: `FreeCADMCP` → `CADPilot`
- Workbench name: "FreeCAD MCP" → "CADPilot"
- Environment variable: `FREECAD_MCP_HOME` → `CADPILOT_HOME`
- Data directory: `~/.freecad-mcp` → `~/.cadpilot`
- Settings file: `freecad_mcp_settings.json` → `cadpilot_settings.json`
- Logger name: `FreeCADMCPserver` → `CADPilot`

### New features (since v0.3.0)

- `datum_plane` and `hull` feature operations in `cad()`
- Assembly session with persistent joints (FreeCAD 1.1 Assembly workbench)
- Connectivity auto-audit after every `cad()` mutation
- Declarative priority trimming (`trim={"winner": ...}`)

### Fixed

- **`execute_code` namespace pollution**: user code now runs in a copy of the
  addon module's globals (same as `execute_code_async`) — assignments can no
  longer corrupt the RPC server's own namespace across calls.
- **Assembly state robustness** (`assembly_state.py`): `load()` returns `None`
  on missing/corrupt/structurally-invalid session files instead of raising
  (consistent with `session_state.load_session`); `save()` is now atomic
  (tmp + replace) so a failed write can't truncate a saved session; the
  `_current` registry is guarded by a lock.
- **`assembly_session` RPC error handling** (`operations/assembly.py`):
  `start` / `add_component` / `mate` / `unmate` / `rollback` now check the
  addon result for `{"success": false}` before consuming fields — a failed
  RPC returns the addon's error message and records nothing (previously
  crashed with `KeyError` after a half-mutated state). `mate` no longer
  crashes when the result carries trim data but the call passed no `trim`.
- **Dev tooling**: the ruff config parsed invalidly (`[tool.ruff.format]
  line-length`), silently disabling all lint/format runs; fixed and the whole
  tree re-linted/reformatted (136 findings resolved).

## v0.3.0

### New features

- **Constrained sketches + PartDesign** — eight new `cad()` feature ops enabling
  the full "variables → sketch → solid → dress-up" parametric chain:
  - `variables` — create/update a Spreadsheet parameter table (`cells: {"A1": [alias, value]}`,
    idempotent); everything downstream binds to it via `=Spreadsheet.alias`.
  - `sketch` — atomic constrained sketch (`Sketcher::SketchObject` inside a
    PartDesign Body, auto-created when absent). `geometry` (line/arc/circle/bspline/point;
    list order = GeoId) + `constraints` (coincident, horizontal, vertical, tangent,
    perpendicular, parallel, equal, symmetric, distance, distance_x, distance_y,
    radius, angle) are applied in one transaction and solved immediately.
    Point references are `[geo_id, "start"|"end"|"center"|"mid"]`; `[-1, *]` is the
    origin. `plane`: "XY"/"XZ"/"YZ" (+`offset`) or `{"face": [obj, "FaceN"]}`.
    Dimensional values accept `=expressions`. Results report `dof` /
    `fully_constrained`; under-constrained sketches succeed with a warning,
    conflicting/failed ones roll back with solver diagnostics
    (`ConflictingConstraints`, `RedundantConstraints`).
  - `pad` / `pocket` — PartDesign extrusion from a sketch (closed profile
    enforced), `length`/`reversed`/`midplane`.
  - `revolution` / `groove` — PartDesign revolve, `axis` ("X"/"Y"/"Z" sketch axes
    or `{"edge": [obj, "EdgeN"]}`) and `angle`.
  - `thickness` / `draft` — dress-up ops; FreeCAD ≥1.1 LinkSub `Base` and
    ≤1.0 `Faces` property layouts both supported. `pull_direction` takes
    `{"edge": [obj, "EdgeN"]}`.
- Live verification script: `scripts/live_sketch_verify.py` (runs against a live
  FreeCAD; builds a parametric bracket and checks volumes, expression
  propagation, failure diagnostics, and undo).

## v0.2.0 (2026-07-30)

### Breaking changes

- Four standalone tools are merged into a single unified **`cad()`** tool
  (nsforge `math()`-style dispatcher, reduces resident tool context):
  - `create_object` → `cad(operation="create_object", doc_name, obj_type, obj_name, ...)`
  - `edit_object` → `cad(operation="edit_object", ...)`
  - `delete_object` → `cad(operation="delete_object", ...)`
  - `execute_operations` → `cad(operation="batch", ops=[...])`
- **Removed** (modeling-only scope):
  - `run_fem_analysis` and all `Fem::` object creation support
    (addon `fem_executor.py` gone; no CalculiX/Gmsh dependency)
  - `get_parts_list` and `insert_part_from_library` (addon `parts_library.py` gone)
  - `reload_document`
- **RPC response format**: `get_objects`, `get_object`, and `list_documents` now
  return `{"success": true, "objects"/"object"/"documents": ...}` instead of bare
  lists/dicts. The MCP client (`freecad_client.py`) handles the conversion
  transparently, so MCP tool callers see the same data — but direct XML-RPC
  consumers must adapt.
- Clients calling the removed/merged tools must switch to `cad()`.

### New features

- **Modeling sessions**: `session_start` / `session_status` / `session_get_steps` /
  `session_rollback` / `session_redo` / `session_add_note` / `session_pause` /
  `session_resume` / `session_list` / `session_complete`.
  - Every committed mutation runs inside a FreeCAD transaction; `session_rollback`
    maps to native `doc.undo()` with log truncation, `session_redo` mirrors FreeCAD
    redo semantics (a new step clears the redo buffer).
  - `execute_code` steps are non-atomic and block rollback unless `force=True`.
  - Sessions persist as JSON under `$CADPILOT_HOME/sessions/` (default
    `~/.cadpilot/sessions/`).
- **Pattern memory**: `save_pattern` / `recall_patterns` — successful workflows can be
  stored and retrieved by keyword search (CJK-safe substring matching).
  `session_complete` can archive a session as a pattern.
- **Runtime introspection**: `inspect_freecad` — inspect an object's properties/methods
  or a dotted-name API docstring without leaving the session.

### Fixed

- **Double coordinate transform in geometry queries**: FreeCAD Shapes carry the
  object's Placement as their internal location, so `BoundBox`, `CenterOfMass`,
  `Vertex.Point`, `Face.Surface` (Axis/Center) and `Face.normalAt` already return
  GLOBAL coordinates. `measure_geometry` / `get_topology` / `get_positioning_info`
  applied `obj.Placement` a second time, returning wrong positions/normals/axes for
  any moved or rotated object (e.g. a rotated fuselage reported its bbox along -Z).
  All manual placement transforms removed; `placement.rotation.angle_deg` now
  actually reports degrees (was radians).
- **`align_shapes` radian/degree bug**: `Face.getAngle()` returns radians but
  `FreeCAD.Rotation(axis, angle)` expects degrees — touch/axis modes rotated by a
  far-too-small angle. Fixed with `math.degrees()`.
- **Expression binding**: in `cad()` create/edit `obj_properties`, string values starting with `=` are bound via the ExpressionEngine (`obj.setExpression`) instead of assigned literally — Spreadsheet-driven parametric design without new tools.
- **Feature operations**: `cad()` gains `boolean`/`fillet`/`chamfer`/`loft`/`sweep`/`mirror`/`pattern` — parametric FreeCAD objects (transactional, rollback-able), with edge/face selectors ("all" / indices / names) fed by `get_topology`.
- **Geometry sensing**: `measure_geometry` (volume/area/bbox/center of mass/validity), `get_topology` (paginated faces/edges/vertices with semantic info for selection), `check_interference` (distance + common volume) — quantitative feedback after each modeling step.
- **Spatial positioning** (the hardest problem in AI-driven CAD assembly):
  - `cad(operation="move")` — relative translate/rotate on top of current Placement
    (solves ~80% of positioning needs without manual coordinate math).
  - `get_positioning_info` — global-coordinate spatial data for a specific face/edge/vertex
    (center, normal, axis, radius, start/end points — all transformed by the object's Placement).
  - `align_shapes` — move an object so one of its elements aligns with a target element on
    another object. Modes: `"touch"` (face-to-face contact, normals opposed), `"center"`
    (center-to-center), `"axis"` (cylindrical axis alignment). Optional `offset` for gap/overlap.
- **Global coordinates everywhere**: `measure_geometry` now returns bounding box and center of
  mass in global coordinates (applies Placement transform). `get_topology` face/edge/vertex
  entries now include global-coordinate data: face `radius`/`axis` (cylindrical/conical/spherical),
  edge `start`/`end` vertices and `radius`/`axis` (circular), vertex global position.
- **Guidance**: mutation responses include lightweight `display_text` suggestions and
  risk warnings (state drift after rollback, non-atomic steps, document closed).

### Bug fixes

- **Consistent edge schema**: closed (full-circle) edges in `get_topology` /
  `get_positioning_info` now always carry an `end` point (equal to `start`) —
  previously the key was absent for single-vertex edges, breaking callers that
  iterate `start`/`end` uniformly.
- **`align_shapes` silent offset**: `offset` is only meaningful in `touch` mode;
  passing a non-zero offset in `center`/`axis` mode now returns a `warning`
  field instead of silently ignoring it.
- **Dead code**: removed an always-overwritten placement computation in
  `_build_move` (`feature_ops.py`).
- **Null shape serialization**: `serialize_shape` now checks `shape.isNull()` in
  addition to `shape is None`, preventing `AttributeError` on objects whose Shape
  property exists but is a null OCCT handle.
- **Mirror feature type**: `_build_mirror` now tries `Part::Mirroring` first and
  falls back to `Part::Mirror` only on type-not-found errors, with a clear
  `ValueError` if neither exists — no more silent `TypeError` on FreeCAD builds
  that only ship one of the two.
- **Thread safety**: `_now()` in `session_state.py` wrapped with a `threading.Lock`
  to prevent rare timestamp collisions in concurrent session operations.
- **Object name normalization**: `_normalize_object_names()` handles both string
  and dict elements from different RPC code paths, fixing `objects_after`
  fingerprint mismatches in session steps.
- **Interference threshold**: `check_interference` common-volume threshold raised
  from `1e-7` to `1e-4` mm³ to avoid false positives from floating-point noise.
- **Face normals**: `get_topology` now computes normals for ALL face types (not
  just `Plane`), using `face.normalAt()` at the face center.

### Improvements

- **Screenshot policy**: screenshots are opt-in per call (`with_screenshot`).
  Precedence: `--only-text-feedback` (hard off) > per-call `with_screenshot` >
  `--with-screenshots` (default-on). Screenshots are capped at 768px on the long edge
  by default to save tokens.
- **Inline screenshots**: `execute_code` and `create_document` now capture
  screenshots in the same GUI dispatch (single RPC round trip) instead of a
  separate `get_active_screenshot` call, halving latency for screenshot-enabled
  workflows.
- **Stability**: read-only RPCs (`get_objects`, `get_object`, `list_documents`) now
  dispatch onto the FreeCAD GUI thread; the XML-RPC client retries once on
  recoverable connection errors.
- **Merged RPC**: mutations and their optional screenshot are fetched in a single
  XML-RPC round trip; mutation results include an `objects` fingerprint (sorted
  object names) used for drift detection.
- **Batch single-recompute**: `cad(operation="batch", ...)` now skips per-object
  `doc.recompute()` and performs a single recompute after all ops, significantly
  faster for large batches.
- **Boolean multi-tool**: `cad(operation="boolean", tool=["Obj1","Obj2",...])`
  now accepts a list of tool objects — they are fused into a temporary compound
  before the boolean operation, enabling multi-body cuts/fuses in one step.
- **ViewObject serialization**: extended with `DisplayMode`, `LineColor`,
  `PointSize`, `LineWidth`, and `DrawStyle` properties for richer visual feedback.
- **Compatibility**: the new MCP server falls back gracefully against older addons
  (single-shot screenshot RPC, optional params); older clients keep working against
  the new addon.
  - Tests: pytest suite (119 tests) covering responses, operations, reconnect, session
  state, pattern store, guidance, cad() dispatch, name normalization, spatial
  positioning (move, get_positioning_info, align_shapes), and global-coordinate
  topology queries.
