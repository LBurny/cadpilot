"""Reference documentation served on demand via the ``operation_help`` tool.

This text lives OUTSIDE tool docstrings on purpose: docstrings are injected
into the AI client's context on every conversation (MCP tools/list), which
caused prompt explosion (~6.6K tokens for 32 tools, cad() alone ~2.4K).
``operation_help`` fetches the full reference only when actually needed.

Docstring discipline: keep each tool's docstring to a 1-3 line summary plus
critical gotchas and a pointer here. tests/test_operation_help.py enforces a
total-size budget as a regression guard.
"""

from __future__ import annotations

CAD_OP_DOCS: dict[str, str] = {
    "multi_agent": """\
multi-agent: several agents share one FreeCAD safely.

Every agent talks to the same FreeCAD over its own MCP server process, and
they interleave on the GUI thread. The rule that keeps them apart: name the
document in every call that has a doc_name argument, and give the two
state-bound calls their document explicitly:

- execute_code: pass doc_name. It binds the wrapper transaction, the step
  journal entry AND App.ActiveDocument (so App.ActiveDocument inside the
  snippet resolves to the declared document). Without it, all three used to
  land on whichever document the OTHER agent's call left active, mixing
  steps into each other's journals. Two automatic bindings now close that:
  an active session binds its own document, and with no session the run
  binds to the document THIS server process last created or mutated (each
  agent drives its own MCP server process, so the two stick to their own
  document). A fallback binding is disclosed in the reply; pass doc_name
  whenever the target differs.
- get_view: pass doc_name. The default frames the foreground tab, which the
  other agent may have switched, so the screenshot shows the wrong model.
- cad / get_objects / get_object / measure_geometry / get_topology /
  set_anchors / assemble / align_shapes / verify_assembly / step_control /
  session_*: already take doc_name — always pass it, never rely on the
  active document.

Screenshots attached to mutations (cad with_screenshot, get_objects, ...)
frame the mutation's own document.

Hard isolation (two agents that must never see each other, separate undo
stacks) still means two FreeCAD instances on different ports; a shared
instance gives correct attribution, not privacy.""",
    "create_object": """\
create_object — create a FreeCAD object.
Required: obj_type, obj_name. Optional: obj_properties.
obj_type starts with "Part::" / "Draft::" / "PartDesign::"
(e.g. 'Part::Box', 'Part::Cylinder', 'PartDesign::Body').

Example (cylinder, height 30, radius 10, moved and rotated):
    {"operation": "create_object", "doc_name": "MyDoc",
     "obj_type": "Part::Cylinder", "obj_name": "Cylinder",
     "obj_properties": {
        "Height": 30, "Radius": 10,
        "Placement": {"Base": {"x": 10, "y": 10, "z": 0},
                      "Rotation": {"Axis": {"x": 0, "y": 0, "z": 1}, "Angle": 45}},
        "ViewObject": {"ShapeColor": [0.5, 0.5, 0.5, 1.0]}}}

Expression binding: an obj_properties string value starting with "=" is bound
as an expression instead of a literal — e.g. {"Length": "=Spreadsheet.width * 2"}.
Edit the cell afterwards and the model updates on recompute.""",
    "edit_object": """\
edit_object — modify object properties.
Required: obj_name, obj_properties. Same expression-binding rule as
create_object ("=..." values bind expressions).""",
    "delete_object": """\
delete_object — remove an object. Required: obj_name.""",
    "batch": """\
batch — many ops in one call (one undo unit, at most one screenshot).
Required: ops (list). Optional: stop_on_error (default False).
Each op: {"action": "create_object"|"edit_object"|"delete_object"|<feature op>,
          ...same fields as the single operation...}
Feature actions in batch: {"action": "fillet", "obj_name": ..., "obj_properties": {...}}.
stop_on_error=true stops at the first failed op (already-run ops stay applied —
it does NOT roll the batch back). With either setting, objects a FAILED op had
already created are removed again, and its per-op result says so
("removed_debris"); feature results carry the same diagnostics (dof,
fully_constrained, warnings) as the single-op call.
Returns JSON with per-op results.""",
    "boolean": """\
boolean — parametric Boolean (obj_name = base object).
Required in obj_properties: op (fuse/cut/common), tool. Optional: name.
Without name the result is named after the op ("Cut"/"Fuse"/"Common"), which
FreeCAD de-duplicates — read the actual name from the response.
tool: single object name OR a list — multiple tools are combined into one
(hidden) Part::Compound that stays linked as the boolean's Tool.""",
    "fillet": """\
fillet — parametric fillet (obj_name = base object).
Required in obj_properties: edges (selector), radius. Optional: name.
Selectors accept "all", an index list [0,2], or a name list ["Edge1"] —
use get_topology to find indices.
A base inside a PartDesign Body yields a PartDesign::Fillet inside that Body
(it follows the Body's Placement and becomes its Tip); a bare Part-level base
yields a Part::Fillet at the document root. A base inside a Body must BE that
Body's Tip: dressing a mid-chain feature is refused, because FreeCAD moves the
Body's Tip onto the new dress-up and would silently drop every later feature
(pockets, patterns).""",
    "chamfer": """\
chamfer — parametric chamfer (obj_name = base object).
Required in obj_properties: edges (selector), size. Optional: name.
Same selector syntax and the same Body-Tip rule as fillet.""",
    "loft": """\
loft — parametric loft through profiles (obj_name names the NEW loft, may be omitted).
Required in obj_properties: profiles (list of >= 2 object names).
Optional: solid (default True), ruled, name.""",
    "sweep": """\
sweep — parametric sweep (obj_name = profile object).
Required in obj_properties: path. Optional: solid, name.""",
    "mirror": """\
mirror — parametric mirror (obj_name = base object).
Optional in obj_properties: plane (XY/XZ/YZ, default XY) or face (selector item), name.""",
    "pattern": """\
pattern — repeat a feature or solid (obj_name = base object).
Required in obj_properties: count (>= 2, or an expression, e.g. "=Vars.n_holes";
an expression stays BOUND, so changing the spreadsheet updates the hole count).
Optional: pattern_type (linear/polar, default linear), spacing, axis (X/Y/Z or
  a direction vector), angle, center, reversed, name.

Two VERY different behaviours, picked by the base object:
- Base is a PartDesign feature (a pad/pocket/… inside a Body): a
  PartDesign::PolarPattern / LinearPattern is created in that Body, repeating
  the FEATURE (e.g. a bolt hole) — one solid with N holes.
- Base is a Part-level solid (a boolean result, a primitive): a Draft array
  replicates the whole object.
PartDesign patterns are refused (clear error, nothing created) when the
transform has no effect — FreeCAD 1.1 cannot drive a PartDesign pattern
reliably through this API, and silently returning one hole instead of six is
worse than failing. Workaround: pattern the profile (one pocket per instance,
e.g. from execute_code) or pattern a Part-level solid with boolean ops.""",
    "move": """\
move — relative Placement change (obj_name = object to move).
Optional in obj_properties (vectors accept [x,y,z] or {"x":..,"y":..,"z":..}):
  translate [dx,dy,dz] — relative translation in GLOBAL coords
  rotate {"axis": [ax,ay,az], "angle": degrees} — relative rotation about the
    object's OWN origin (its Placement Base), NOT the world origin; translate
    first if you want to orbit another point
  placement — absolute Placement override (same format as obj_properties.Placement)""",
    "variables": """\
variables — create/update a Spreadsheet parameter table (idempotent).
obj_name = spreadsheet name (default "Spreadsheet", may be omitted).
Required in obj_properties: cells — maps a cell to [alias, value]; values are
numbers, "=formula" strings, or text. Everything downstream references cells
as =Spreadsheet.alias.""",
    "sketch": """\
sketch — atomic constrained sketch (Sketcher::SketchObject inside a PartDesign
Body; the body is auto-created when absent). Geometry + constraints in ONE
transaction; the solver runs immediately.
obj_name = sketch name (default "Sketch", may be omitted).
Required in obj_properties: geometry.
Optional: plane, offset, body, construction, external, constraints.

- plane: "XY" / "XZ" / "YZ" (with optional offset along the plane normal),
  {"face": ["ObjName", "FaceN"], "offset": 0} to sketch on an existing solid
  face, or {"datum": "DatumPlaneName"} to sketch on a datum plane.
  PREFER A DIRECTION OVER A FACE NAME: {"face": ["ObjName", "+Z"]} (also -Z,
  +X/-X, +Y/-Y, top/bottom/left/right/front/back, MaxX/MinZ/…) resolves to the
  planar face facing that way. Face names (Face1, Face2, …) are re-derived
  after every feature, so a name read off one feature can silently mean a
  different face on the next one — the sketch then lands on the wrong plane and
  its pocket cuts air while still reporting success.
  {"face": [...], "center": true} puts the sketch origin at the MIDDLE of the
  face; without it the origin sits on the face's parametric origin, which is a
  CORNER on rectangular faces (a circle meant for the middle of a 100x60 side
  face lands at its corner and the cut is clipped by the part's edge).
  The sketch result echoes the resolved `plane` (object, face name, center,
  normal, centered) so a surprise pick is visible — a direction token picks the
  FARTHEST face facing that way.
- geometry: list of items; the list order is the GeoId used in constraints:
    {"type": "line", "from": [x,y], "to": [x,y]}
    {"type": "arc", "center": [x,y], "radius": r, "start_angle": deg, "end_angle": deg}
    {"type": "circle", "center": [x,y], "radius": r}
    {"type": "bspline", "points": [[x,y],...], "periodic": false}
    {"type": "point", "at": [x,y]}
- construction: list of GeoIds treated as construction geometry.
- external: list of [obj_name, "EdgeN"|"VertexN"] — external geometry
  (cross-part parametric references). The i-th entry takes GeoId -(3+i) and
  constraints may reference its start/end points, e.g. [-3, "start"]
  (mid/center are NOT exposed on external geometry — rejected up front).
  Targets outside the sketch's PartDesign Body are bridged automatically via
  a SubShapeBinder (PartDesign scope rule).
- constraints: list of {"type": ..., "items": [...], "value": ...}.
  Point references are [geo_id, "start"|"end"|"center"|"mid"].
  Supported types: coincident, horizontal, vertical, tangent, perpendicular,
  parallel, equal, symmetric, distance, distance_x, distance_y, radius, angle.
  value accepts numbers or "=expressions".
  distance_x/distance_y are SIGNED and measured FROM the first referenced point
  TO the second, so [[0,"start"],[-1,"center"]] with +30 puts the origin at
  (−30, ·) — swapping the two items flips the sign. Write the constraint in the
  order you mean it.
- Reusing an existing name does NOT overwrite: FreeCAD de-duplicates
  ("HoleProfile" → "HoleProfile001"); the response carries the actual name,
  so reuse it from there.
- Sketch coordinates are 2D [x, y] in mm in the plane's local frame.
- Result: fully constrained sketches report fully_constrained: true;
  under-constrained ones succeed with a warning; conflicting/failed sketches
  are rolled back with solver diagnostics.

Example (parametric plate: variables -> sketch -> pad):
    {"operation": "variables", "doc_name": "D", "obj_name": "Spreadsheet",
     "obj_properties": {"cells": {"A1": ["width", 60], "A2": ["thick", 8]}}}
    {"operation": "sketch", "doc_name": "D", "obj_name": "Profile",
     "obj_properties": {
       "plane": "XY",
       "geometry": [
         {"type": "line", "from": [0, 0], "to": [60, 0]},
         {"type": "line", "from": [60, 0], "to": [60, 30]},
         {"type": "line", "from": [60, 30], "to": [0, 30]},
         {"type": "line", "from": [0, 30], "to": [0, 0]}],
       "constraints": [
         {"type": "coincident", "items": [[0, "end"], [1, "start"]]},
         {"type": "coincident", "items": [[1, "end"], [2, "start"]]},
         {"type": "coincident", "items": [[2, "end"], [3, "start"]]},
         {"type": "coincident", "items": [[3, "end"], [0, "start"]]},
         {"type": "horizontal", "items": [[0]]},
         {"type": "horizontal", "items": [[2]]},
         {"type": "vertical", "items": [[1]]},
         {"type": "vertical", "items": [[3]]},
         {"type": "coincident", "items": [[0, "start"], [-1, "center"]]},
         {"type": "distance_x", "items": [[0, "start"], [0, "end"]],
          "value": "=Spreadsheet.width"},
         {"type": "distance_y", "items": [[1, "start"], [1, "end"]], "value": 30}]}}
    ([-1, "center"] refers to the sketch origin.)
    {"operation": "pad", "doc_name": "D", "obj_name": "Profile",
     "obj_properties": {"length": "=Spreadsheet.thick"}}""",
    "pad": """\
pad — extrude a closed sketch profile (obj_name = profile sketch).
Optional in obj_properties: length (default 10), reversed, midplane, body, name.
Numeric params accept "=expressions". The profile must have a closed wire.
A NEGATIVE length is legal and extrudes the other way (the result warns).
A profile that does not touch the base still succeeds (the Body allows
compounds) but leaves a floating solid — the result warns about it.
Attachment fusion: on a face-attached sketch, pad FUSES into the supporting
solid automatically — do not pad-then-boolean.""",
    "pocket": """\
pocket — cut a closed sketch profile out of a solid (obj_name = profile sketch).
Optional: length (default 10), reversed, midplane, body, name.
A NEGATIVE length is legal and cuts the other way (the result warns).
Attachment fusion: on a face-attached sketch, pocket CUTS the supporting
solid via attachment — no boolean needed.""",
    "revolution": """\
revolution — revolve a closed profile (obj_name = profile sketch).
Optional: axis ("X"/"Y"/"Z" sketch axes, or {"edge": ["ObjName", "EdgeN"]}),
angle (degrees, default 360), body, name.""",
    "groove": """\
groove — subtractive revolution (obj_name = profile sketch).
Same axis/angle params as revolution.""",
    "thickness": """\
thickness — shell a solid (obj_name = base solid feature).
Required in obj_properties: faces (selector), value. Optional: reversed, body, name.
faces uses the selector syntax ("all" / index list / name list) and also
accepts DIRECTION tokens ("+Z", "top", "MaxX", …), which is the stable way to
name a face (FaceN is re-derived after every feature). A direction picks the
FARTHEST face facing that way, so on a stepped part check the echoed
`resolved` (face name + center + normal) before trusting the result.
faces='all' is refused: those are the faces to OPEN, and removing all of them
leaves the solid unchanged.
DIRECTION: the wall is offset along the selected face's normal; when that
normal points OUT of the material the part grows (live: a 100x60x40 box came
back 3 mm bigger in every axis, walls sitting on the outside). For an inward
(hollowing) shell pass reversed=true — the result then carries a warning when
material was added outside, so a wrong-direction shell is not silent.""",
    "draft": """\
draft — taper faces (obj_name = base solid feature).
Required in obj_properties: faces (selector), angle.
Optional: neutral_plane, pull_direction ({"edge": ["ObjName", "EdgeN"]}), body, name.
faces accepts direction tokens like thickness; the result echoes the resolved
faces (name/center/normal).""",
    "datum_plane": """\
datum_plane — PartDesign datum plane (obj_name = plane name, default
"DatumPlane", may be omitted).
Required in obj_properties: plane — "XY"/"XZ"/"YZ" (attached to the body
origin) or {"face": ["ObjName", "FaceN"]} (attached to an existing face;
"+Z"/"top"/"MaxX"/… direction tokens resolve like a sketch's plane.face).
Add {"face": [...], "center": true} to put the plane's origin at the MIDDLE of
the face (the default is the face's parametric origin — a corner on
rectangular faces; sketches attached to the datum then land there).
The result echoes the resolved face (name/center/normal, and the datum's own
support), so a surprise direction pick is visible.
Optional: offset (mm along the plane normal), body.
Sketch on it with plane={"datum": name}.""",
    "hull": """\
hull — multi-view 2D-to-3D visual hull (obj_name = result name, default "Hull",
may be omitted).
Required in obj_properties: sketches — a list of 2-3 view-profile sketch
names or {"top": .., "front": .., "side": ..} (convention: Top=XY, Front=XZ,
Side=YZ; ONE closed outer profile per sketch). Optional: margin (mm; pads the
extrusion extent, default 5% of the views' bounding diagonal, min 1mm).

The solid is the intersection of the views extruded along their sketch
normals. Result is a static Part::Feature; re-running with the same name
replaces the Shape in place — edit a view sketch and re-run to iterate.
Empty intersection raises an error ("view profiles do not overlap").
v1 limits: view sketches at the global origin; hole wires are not subtracted.""",
    "assembly_session": """\
assembly_session — independent assembly state machine with PERSISTENT joints
(FreeCAD Assembly workbench) — the mate-based counterpart to one-shot assemble.

Workflow: start(ground=part) -> add_component(part) per part ->
mate(a, b, joint_type, trim?) per joint -> solve -> verify -> complete.
Joints persist in the document: move a parent part, call solve, and children
follow. rollback(to_step) un-does joints/trims and restores placements
atomically.

operations:
  start          — doc_name + part (ground part, gets grounded)
  add_component  — part (wrapped as App::Link inside the assembly)
  mate           — a, b refs; joint_type; optional trim + name
  solve          — re-solve all joints, returns per-joint residuals
  unmate         — delete one joint by name (joint=name)
  rollback       — to_step (see status for step numbers)
  verify         — residuals + islands + interference + mate gap profile
  status         — components/joints/steps of the active session
  complete       — close the session (document keeps the joints)

A mate ref is {"part": <name>} plus exactly ONE of:
  "face": "FaceN"   — direct face reference
  "anchor": <name>  — resolve a named anchor (get_anchors/set_anchors)
  "point": [x,y,z]  — global point; nearest planar face is used
Anchors and points must sit ON the part: the ref resolves to the nearest
PLANAR FACE within 1 mm. The auto faceN_center anchors lie on their face;
the axis_*/bbox_* anchors do not, so use those for assemble/verify checks
rather than as mate refs.
The contact point maps to the nearest VERTEX of the face, which decides WHERE
on the face the mate lands (GUI click semantics). On a symmetric face the
nearest vertex to the center is arbitrary (a square face lands at a corner),
so the mate result reports the landing vertices and warns when the landing
is far from the face center — control it with "point_on_face": [x,y,z]
(a face-ref modifier picking the vertex near that global point), an anchor,
or a point ref.

joint_type: fixed (default) / revolute / cylindrical / slider / ball /
distance / parallel / perpendicular / angle.

trim={"winner": "inserted"|"base"} declares priority trimming: the loser gets a
non-destructive baked cut (a separate TrimCut object built from the winner's
volume; the winner keeps its shape and dims); rolled back with the mate.""",
    "assemble": """\
assemble — one-shot anchor snapping (ONE transaction).
Each mate: {"obj", "anchor", "target", "target_anchor",
            "mode": "center"|"touch"|"axis", "offset": float=0}.
  center — anchor points coincide (translation only).
  touch  — directions oppose (face-to-face); the point lands at
           target_pos + target_dir*offset.
  axis   — directions parallel (axle-in-hole); same landing rule.
Mates run in order; later mates see earlier moves. Every mate's residual
(mm, plus degrees for touch/axis) is measured AFTER the move and returned.
A mate whose residual exceeds tolerance fails: stop_on_error=True aborts the
whole transaction (nothing moves); False commits the passing mates.
For PERSISTENT joints use assembly_session instead.""",
    "step_plan": """\
step_plan — submit a plan to FreeCAD's step journal WITHOUT executing it.

Each entry is a cad() argument dict, so the panel can run it later:
    {"operation": "create_object", "obj_type": "Part::Box", "obj_name": "Base",
     "description": "base plate"}
    {"operation": "pad", "obj_name": "Sketch", "obj_properties": {"Length": 20}}

The plan lands in the document's journal as `planned` steps and shows up in
the CADPilot Steps panel; `description` becomes the panel's plan title, and a
per-step `description` becomes its row label — write both for the human
watching the panel. Nothing touches the model until a step is released — by
the user clicking Next in the panel, or via step_control. A new cad() call
discards whatever part of the plan has not run yet, because it was planned
against a document state that no longer exists.

Batch steps work too: {"operation": "batch", "ops": [...]} runs the ops in one
transaction as one step.""",
    "step_control": """\
step_control — run, review, and edit steps in a document's journal.

Execution:
  run_next      execute the first planned step
  run_all       execute planned steps until one fails or none remain
  run_to        execute until step `index` is done
  replay        roll back to `index` (default 0) and re-run everything —
                rebuilds the model from the journal after manual edits

Review loop (the point of the panel: plan, release, review, fix):
  accept        mark done step `index` as reviewed; accepted steps are a soft
                lock — rollback_to / reexecute / replay across them need
                force=true (params.on=false un-accepts)
  snapshot      bookmark the current state as the accepted baseline — for
                "the good steps are reviewed, I did the complex part by
                hand, continue from here": done + accepted (soft-locks prior
                steps), non-atomic; objects_before vs objects_after names
                what the journal missed. params.note describes it;
                params.accept_done=true also accepts every done step in
                the same call
  reject        undo step `index` and DROP everything from it onward (done
                steps are undone, planned ones forgotten — the tail was
                authored against step `index` existing). params.reason is
                logged. Reject never stops at accepted steps: it is the
                deliberate act of destruction
  update        edit a PLANNED/FAILED step without running it: params merge
                top-level (obj_properties is replaced wholesale — send the
                full dict), params.label renames the row
  reexecute     roll back to just before `index`, then run it with `params`
                merged in; the planned tail survives (reject drops it)

Housekeeping:
  rollback_to   put the model back at step `index` (0 = before every recorded
                step); records stay and go back to planned. The undo result is
                VERIFIED, not assumed: when the undo stack cannot reach the
                target (or manual edits sit interleaved on it) the journal
                REBUILDS instead — removes what the journal built, re-runs
                steps 1..index — and reports `restored: native | rebuild |
                partial` plus `warnings`; objects that predate the journal
                are never removed
  insert        add steps (params.steps) after `index`; the planned tail is
                insert/append-only — to change history, reject and re-plan
  clear_plan    drop not-yet-executed steps (never touches the model)
  reset         forget the whole journal (pass confirm=true)
  status        full records incl. params + meta (plan description)

Every mutating action's reply carries a compact `journal` snapshot (counts,
drift flag, per-step index/state/label/error/accepted) — one call tells you
the new state; use status only when you need the full params.

`rollback_to`/`reexecute`/`replay` refuse to cross a non-atomic step unless
force=true. An execute_code run that changed the document is wrapped in a
transaction and counts as atomic AND re-runnable (its code is stored, so replay
re-executes it); only a read-only execute_code run stays non-atomic and is
skipped by replay. Re-execution is available for modeling steps (create_object /
edit_object / delete_object / batch / execute_code / PartDesign & Part
features) and for the assembly toolchain (assembly / assemble / align_shapes /
set_anchors), which journals its full payload and re-runs for real.""",
    "get_addon_log": """\
get_addon_log — read the FreeCAD addon's in-memory debug log.

Every record carries: seq (strictly increasing within this FreeCAD session),
time, level, name (logger, e.g. CADPilot.rpc), thread, request ("req#42" —
the RPC that caused it, carried across onto the GUI thread), message, detail
(traceback or an extra payload).

The request id is what makes cross-thread work followable: one `-> cad(...)`
line on the RPC thread and the `open transaction` / `GUI task ran after Nms`
lines it caused all share the same req#.

Incremental reads: pass since_seq = the seq of the last record you saw to get
only what is new (the `status` field reports level, buffered/capacity, and the
log file path).

What is worth grepping for when debugging:
  "GUI dispatch timed out"   — the GUI thread never picked the task up
  "wake/heartbeat chain"     — timeout with an idle GUI thread: the waker died
  "mouse guard:"             — a drag (or a phantom hold) deferred the queue
  "aborted transaction"      — a modeling op failed and was rolled back
  "journal <op> failed"      — a steps-panel button did not take effect

Levels: DEBUG has per-request arg summaries and timings; INFO is the default
and covers state changes; WARNING+ is also mirrored into FreeCAD's Report
View. The same records go to a rotating file under FreeCAD's user data dir
(<user data>/CADPilot/logs/cadpilot.log), so they survive a crash.""",
    "diagnose": """\
diagnose — find out why CADPilot cannot talk to FreeCAD.

Runs entirely on the MCP side, so it still answers while FreeCAD is down,
frozen, or was never started (get_addon_log and every other tool need a live
connection). From the outside it probes: the RPC endpoint (ping on a 5s
timeout, plus a raw TCP connect), the FreeCAD process, who listens on the
port, the installed addon (symlink or copy, complete or not) in each FreeCAD
user-data dir, the bootstrap crash log, the addon log's freshness, and
cadpilot_settings.json.

Read the verdict, then work down this order — identical on Windows, macOS
and Linux:
  1. Is FreeCAD running? A closed port with no process means nothing to
     talk to.
  2. Is the RPC server started? In FreeCAD: the CADPilot toolbar's "RPC
     Server" toggle, or auto_start_rpc in the settings (auto-start is
     evaluated at startup only, so enabling it later needs a restart).
  3. Is the addon installed in the user-data dir of the FreeCAD version
     that is actually running? FreeCAD 1.x uses a versioned level, e.g.
     v1-1 (Windows: %APPDATA%\\FreeCAD\\v1-1\\Mod, macOS:
     ~/Library/Application Support/FreeCAD/v1-1/Mod, Linux:
     ~/.local/share/FreeCAD/v1-1/Mod). An addon in the wrong level never
     loads, and older builds wrote straight into the config root.
  4. Did it load at all? A bootstrap crash is written to initgui_debug.log
     — in the addon dir, or next to the FreeCAD executable when __file__ is
     unavailable under FreeCAD's bare exec(). When the addon fails to load
     there is no workbench entry, no RPC server and a stale addon log; the
     crash log names the cause.
  5. Did you change anything? Addon files, the MCP client config and the MCP
     tool list are only picked up on a restart: restart FreeCAD, then the
     MCP client (it builds tools/list once, at startup).

A listening port that does not answer ping is the one case that is not a
setup problem: FreeCAD is up but its GUI thread is busy or wedged (modal
dialog, long recompute, deadlock). The addon log stays readable exactly
there — grep it with get_addon_log before restarting.""",
    "session": """\
session — modeling session bound to a document (step recording + rollback).

With a session active, every committed cad() mutation on its document is
recorded as a transaction-backed step, so rollback can backtrack instead of
delete-and-rebuild. A mutating execute_code run is an atomic step too; a
read-only run is not recorded.

Actions:
  start       bind a session to doc_name (create_document=true creates the
              document first; name gives a human label). Returns session_id.
              One active session per server process.
  status      step count, document state, next-step suggestions, risks
              (GUI-edit drift, non-atomic steps, connectivity islands)
  get_steps   full step records + notes + redo buffer
  rollback    undo everything after to_step (keep 1..to_step; 0 = undo all).
              to_step is REQUIRED so a bare call cannot wipe the session.
              force=true rolls back across non-atomic steps (risky: undo may
              revert the wrong change). Removed steps sit in a redo buffer.
              success=false means the model did NOT reach the target state
              (the undo came up short or the object set does not match);
              warnings say what was left behind.
  redo        restore n rolled-back steps (valid until a new cad() call).
              Fails loudly when FreeCAD's redo stack no longer holds this
              document's transactions — the buffer and the document are then
              out of sync, and nothing is changed.
  add_note    attach an insight to the log (note; note_type: observation |
              assumption | limitation | correction)
  pause       persist and release the active session (returns session_id)
  resume      reactivate a persisted session (session_id; see list); warns
              when its document is no longer open
  list        all persisted sessions, most recently updated first
  complete    finish: store the workflow as a reusable pattern (recall it
              with recall_patterns) and optionally save the document
              (save=true, save_path, description, tags)

Undo window: FreeCAD keeps the last 20 undo steps per document by default
(Preferences > General > Document), so a session longer than that cannot roll
back past the window. A rollback that comes up short says so instead of
pretending; the FreeCAD-side step journal (step_control rollback_to) can still
rebuild the model there.
""",
    "screenshots": """\
screenshots: get_view is the only capture tool — everything else is text.

get_view always captures: the PNG is written under
$CADPILOT_HOME/screenshots/ (newest 100 kept) and the response carries only
the file path as text ("Screenshot saved to <path> (WxH). View it with your
file-reading tool."). No image data enters the conversation; a multimodal
client opens the PNG with its own file tool. If saving fails, the response
says so in text.

--only-text-feedback forbids screenshots entirely (get_view then returns a
text notice). Captures are capped at 384px on the long edge unless
width/height are given; TechDraw and Spreadsheet views yield no screenshot
at all.""",
}

HELP_TOPICS: dict[str, str] = {
    **{
        op: f'cad(operation="{op}")'
        for op in CAD_OP_DOCS
        if op
        not in (
            "assembly_session",
            "assemble",
            "step_plan",
            "step_control",
            "get_addon_log",
            "multi_agent",
            "session",
            "screenshots",
        )
    },
    "assembly_session": "assembly_session tool (persistent-joint assembly)",
    "assemble": "assemble tool (one-shot anchor snapping)",
    "step_plan": "step_plan tool (submit a plan without executing it)",
    "step_control": "step_control tool (run / roll back / re-run steps)",
    "get_addon_log": "get_addon_log tool (read the addon's debug log)",
    "multi_agent": "running several agents against one FreeCAD without cross-talk",
    "session": "session tool (step recording + rollback state machine)",
    "screenshots": "get_view is the only capture tool (file path delivery)",
}


def operation_help_text(operation: str | None) -> str:
    """Resolve a help topic; unknown/empty -> overview of available topics."""
    if operation:
        doc = CAD_OP_DOCS.get(operation)
        if doc is not None:
            return doc
        hint = f"unknown operation '{operation}'.\n\n"
    else:
        hint = "Pass an operation name for its full parameter reference.\n\n"
    lines = ["Available help topics:"]
    for key in CAD_OP_DOCS:
        lines.append(f"  - {key}")
    return hint + "\n".join(lines)
