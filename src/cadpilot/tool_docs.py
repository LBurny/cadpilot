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
Returns JSON with per-op results.""",
    "boolean": """\
boolean — parametric Boolean (obj_name = base object).
Required in obj_properties: op (fuse/cut/common), tool. Optional: name.
tool: single object name OR a list — multiple tools are combined into one
(hidden) Part::Compound that stays linked as the boolean's Tool.""",
    "fillet": """\
fillet — parametric fillet (obj_name = base object).
Required in obj_properties: edges (selector), radius. Optional: name.
Selectors accept "all", an index list [0,2], or a name list ["Edge1"] —
use get_topology to find indices.""",
    "chamfer": """\
chamfer — parametric chamfer (obj_name = base object).
Required in obj_properties: edges (selector), size. Optional: name.
Same selector syntax as fillet.""",
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
pattern — parametric pattern (obj_name = base object).
Required in obj_properties: count.
Optional: pattern_type (linear/polar), spacing, axis, angle, center, name.""",
    "move": """\
move — relative Placement change (obj_name = object to move).
Optional in obj_properties:
  translate {"x":..,"y":..,"z":..} — relative translation in GLOBAL coords
  rotate {"axis": {"x","y","z"}, "angle": degrees} — relative rotation
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
Attachment fusion: on a face-attached sketch, pad FUSES into the supporting
solid automatically — do not pad-then-boolean.""",
    "pocket": """\
pocket — cut a closed sketch profile out of a solid (obj_name = profile sketch).
Optional: length (default 10), reversed, midplane, body, name.
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
faces uses the selector syntax ("all" / index list / name list).""",
    "draft": """\
draft — taper faces (obj_name = base solid feature).
Required in obj_properties: faces (selector), angle.
Optional: neutral_plane, pull_direction ({"edge": ["ObjName", "EdgeN"]}), body, name.""",
    "datum_plane": """\
datum_plane — PartDesign datum plane (obj_name = plane name, default
"DatumPlane", may be omitted).
Required in obj_properties: plane — "XY"/"XZ"/"YZ" (attached to the body
origin) or {"face": ["ObjName", "FaceN"]} (attached to an existing face).
Optional: offset (mm along the plane normal), body.
Sketch on it with plane={"datum": name}.""",
    "hull": """\
hull — multi-view 2D→3D visual hull (obj_name = result name, default "Hull",
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
The contact point maps to the nearest vertex of the face, which decides WHERE
on the face the mate lands (GUI click semantics).

joint_type: fixed (default) / revolute / cylindrical / slider / ball /
distance / parallel / perpendicular / angle.

trim={"winner": "inserted"|"base"} declares priority trimming: the loser is
cut by a non-destructive Part::Cut (the winner keeps its shape and dims);
rolled back together with the mate.""",
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
}

HELP_TOPICS: dict[str, str] = {
    **{
        op: f'cad(operation="{op}")'
        for op in CAD_OP_DOCS
        if op != "assembly_session" and op != "assemble"
    },
    "assembly_session": "assembly_session tool (persistent-joint assembly)",
    "assemble": "assemble tool (one-shot anchor snapping)",
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
