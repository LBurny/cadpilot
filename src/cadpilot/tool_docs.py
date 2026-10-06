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
tool: single object name OR a list. Several tools are a REAL multi-boolean:
fuse/cut aggregate them with Part::MultiFuse (used as the hidden Tool for cut)
and common intersects ALL of them (Part::MultiCommon). A compound Tool must not
be used: it does not merge overlapping tools (a box+arm+knuckle fuse measured
17462 mm^3 against the true 15765), and for common it flips the meaning to
base ∩ (T1 ∪ T2) instead of base ∩ T1 ∩ T2.""",
    "fillet": """\
fillet — parametric fillet (obj_name = base object).
Required in obj_properties: edges (selector), radius. Optional: name.
Selectors accept "all", an index list [0,2], or a name list ["Edge1"] —
use get_topology to find indices.
A base inside a PartDesign Body yields a PartDesign::Fillet inside that Body
(it follows the Body's Placement and becomes its Tip); a bare Part-level base
yields a Part::Fillet at the document root, and that base object's Visibility
is turned OFF (the result is the same solid with rounded edges, so showing
both means two overlapping parts — the reply names it as hidden_base; its
data is untouched). A base inside a Body must BE that
Body's Tip: dressing a mid-chain feature is refused, because FreeCAD moves the
Body's Tip onto the new dress-up and would silently drop every later feature
(pockets, patterns).
Many edges in one call can defeat OCC: live on 1.1.4 the six bore rims of a
patterned plate failed at 0.5 mm while every rim was fine on its own. When a
multi-edge call does fail, the error says what the retry found (each edge alone
succeeds → split the edges across several calls; specific edges fail alone →
name them, try a smaller radius). A single invalid first recompute is retried in
place before failing, since a stale recompute alone can produce it.""",
    "chamfer": """\
chamfer — parametric chamfer (obj_name = base object).
Required in obj_properties: edges (selector), size. Optional: name.
Same selector syntax, the same Body-Tip rule and the same bare-base hiding as
fillet.""",
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
A polar pattern turns about the AXIS LINE it references, and without `center`
that is the body's own X/Y/Z origin axis: a bolt circle drawn around a face
centre then rotates about the body origin, so occurrences that fall outside the
material are clipped away silently (live: a hole at (60,35) about the origin
gave 1 clean hole and 1 half-hole where 6 were asked for, reported as success).
Pass center: [x, y, z] whenever the rotation centre is not the body origin —
the op builds a datum line there and uses that as the axis. A linear pattern
has no centre: spacing + axis alone decide where the copies land.
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
    "color": """\
color — set appearance: color, transparency, line, display (obj_name = the
object, or "*" for every object in the document).
Optional in obj_properties (at least one required):
  color        — the shape color. Accepted forms, shared with
                 create_object/edit_object's ShapeColor:
                   [r,g,b] / [r,g,b,a] floats 0..1
                   [r,g,b] ints 0-255 (any component > 1 switches to 0-255)
                   "#rrggbb" / "#rrggbbaa"
                   a name: black white grey/gray red orange yellow green
                           blue steel
  transparency — PERCENT 0-100 (FreeCAD's unit). 50 is 50%, not 0.5.
  line_color   — edge color, same forms as color
  line_width   — edge width in px (> 0)
  point_size   — vertex size in px
  display_mode — "Flat Lines" | "Shaded" | "Wireframe" | "Points"
  draw_style   — "Solid" | "Dashed" | "Dotted" | "Dashdot"
  visible      — false hides the object, true shows it
  objects      — a list of extra object names to color the same way

The color is written to the ViewObject and writes THROUGH to ShapeAppearance,
FreeCAD 1.1's persisted per-shape material, so it survives save/reopen. A
PartDesign feature is redirected to its owning Body (a feature's own
ViewObject is hidden, so coloring it changes nothing you can see), and the
reply's `colored` list names every object actually painted with the values
read back off the view provider.

Two FreeCAD rendering rules the reply reports instead of hiding:
  - an object carrying a PER-FACE material list is not repainted by a property
    write, so the op collapses it to one uniform entry (whole-object colour is
    what this op promises) and says so in `normalized_appearance`;
  - a PartDesign feature that advances a Body's Tip rebuilds the Body's display
    with FreeCAD's default material, so a colour set EARLIER visually vanishes
    until the appearance is re-applied. The engine re-applies it automatically
    for every feature it creates, so "colour, then pad/fillet" keeps its colour.

With "*" the objects that cannot be painted are SKIPPED and reported (a
Spreadsheet's view provider has no color at all, and `variables` puts one in
every parametric document) — see `skipped` and the warning in the summary. An
object you NAME is never skipped: if it cannot be painted, the step fails and
rolls back. The per-object lists are capped at 20 rows; `colored_count` and
`skipped_count` always carry the real numbers.

Example (paint one part, then the whole document):
    {"operation": "color", "doc_name": "MyDoc", "obj_name": "Body",
     "obj_properties": {"color": "steel", "transparency": 20}}
    {"operation": "color", "doc_name": "MyDoc", "obj_name": "*",
     "obj_properties": {"color": "#1a6ecc", "line_width": 1.5}}

A color step is a normal undoable/rollback-able/replayable step. Note: a
manual color change made in FreeCAD's GUI afterwards is NOT synced back into
the step's parameters (unlike geometry), so reexecute/replay re-applies the
recorded color.""",
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
  A base-plane name means FreeCAD's own origin plane, identically to a datum
  plane made on it: XY has x->+X, y->+Y, normal +Z; XZ has x->+X, y->+Z,
  normal -Y; YZ has x->+Y, y->+Z, normal +X. So a profile drawn upward in y on
  XZ rises along +Z, and offset moves along the plane's normal (+offset on XZ
  goes toward -Y). The result echoes x_axis/y_axis/normal for the plane.
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
  Naming the BODY is allowed and is resolved to its Tip: the Body's Shape IS
  its Tip's result, so an attachment naming the Body itself closes a dependency
  cycle the moment a feature built on this profile joins the body, and FreeCAD
  reports that as an unreadable shape on the new feature (not as a cycle).
  The sketch result echoes the resolved `plane` (object, face name, center,
  normal, centered) plus `sketch_origin` — where the sketch's origin actually
  landed, which is NOT the reported face center unless you passed
  `"center": true` (a face's parametric origin is a CORNER on a rectangular
  face, so a profile drawn around the origin hangs off the part). A direction
  token picks the FARTHEST face facing that way, and the echo lists
  `same_direction` alternatives.
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
  Item shapes per type (a whole-geometry reference is the GeoId or [GeoId]):
    coincident     [[g,p], [g,p]]                     (two points)
    horizontal     [g]        vertical     [g]        (one edge)
    tangent/perpendicular/parallel/equal  [g]  or [g, g]
    symmetric      [[g,p], [g,p], [g]|  [g,p]]        (about a line or a point)
    distance       [g] | [[g,p],[g,p]] | [[g,p], g]
    distance_x / _y  [[g,p]] (that point's x/y from the ORIGIN, the usual way
                     to place a wall at an absolute coordinate) |
                     [g] (the edge's own horizontal/vertical EXTENT, i.e. the
                     separation of its endpoints along that axis — NOT its
                     position: on a VERTICAL line that extent is 0, so pairing
                     [g] with a `vertical` constraint conflicts) |
                     [[g,p],[g,p]]
    radius         [g]        angle   [g] | [g, g]
  A bare GeoId (not wrapped in a list) is accepted wherever one edge is meant.
  distance_x/distance_y are SIGNED and measured FROM the first referenced point
  TO the second, so [[0,"start"],[-1,"center"]] with +30 puts the origin at
  (−30, ·) — swapping the two items flips the sign. Write the constraint in the
  order you mean it.
  A profile of N lines needs its closing point coincidences AND the
  horizontal/vertical constraints for each axis-aligned edge (a rectangle =
  4 lines + 4 coincidences + 2 horizontal + 2 vertical), or the solver leaves
  rotational/positional degrees of freedom and the sketch reports
  fully_constrained: false.
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
through_all (true) asks for PartDesign's parametric through-all instead of a
length, and `length` is then ignored (it is meant for cuts, so it rarely makes
sense on a pad). pad_type: only "length" is accepted — the other PartDesign
pad modes are unsupported, and the error names through_all for through cuts.
Numeric params accept "=expressions". The profile must have a closed wire.
A NEGATIVE length is legal and extrudes the other way (the result warns).
A profile that does not touch the base still succeeds (the Body allows
compounds) but leaves a floating solid — the result warns about it.
Attachment fusion: on a face-attached sketch, pad FUSES into the supporting
solid automatically — do not pad-then-boolean.""",
    "pocket": """\
pocket — cut a closed sketch profile out of a solid (obj_name = profile sketch).
Optional: length (default 10) OR through_all (true = PartDesign's parametric
through-all, and `length` is then ignored), reversed, midplane, body, name.
pad_type: only "length" is accepted — other PartDesign pocket modes are
unsupported, and the error names through_all for through cuts.
A NEGATIVE length is legal and cuts the other way (the result warns).
through_all is the right choice for a hole that must stay open when the model
gets thicker: a numeric length only goes "through" while the body is thinner
than it, and silently leaves a floor once it is not.
The DIRECTION is decided from the geometry: FreeCAD's own default points away
from the solid when the profile sits on the body's start plane, which removes
0 mm^3, so if the default cuts nothing while the opposite direction removes
material the opposite is applied and the reply's warnings say so. Pass
reversed explicitly to decide it yourself; midplane is never touched.
Attachment fusion: on a face-attached sketch, pocket CUTS the supporting
solid via attachment — no boolean needed.""",
    "revolution": """\
revolution — revolve a closed profile (obj_name = profile sketch).
Optional: axis (default "Y" = the sketch's in-plane V axis, the usual lathe
setup where the profile's y is the axial position and x the radius; "X" = the
in-plane H axis; {"edge": ["ObjName", "EdgeN"]} for an external axis),
angle (degrees, default 360), reversed, body, name.
axis "Z" is the profile's own normal and is REFUSED: revolving a planar
profile about its normal keeps every point in the drawing plane, so it can
only ever produce a flat zero-volume shell (FreeCAD still reports success).
A profile that CROSSES its revolve axis is refused too — FreeCAD's own reason
("Revolve axis intersects the sketch") is carried into the error.""",
    "groove": """\
groove — subtractive revolution (obj_name = profile sketch), the revolved
pocket: it cuts the swung profile OUT of the body.
Optional: axis (default "Y", same forms as revolution), angle (degrees,
default 360), reversed, body, name. The axis-refusal rules match revolution:
the profile's own normal ("Z") and a profile crossing the axis are refused.""",
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
The contact point follows FreeCAD's own click rules and decides WHERE on the
face the mate lands: a face ref without a hint lands on the FACE CENTER (the
face-name marker the GUI writes for a plain selection), and it is what makes
a fixed mate of two circular faces concentric; with "point_on_face" it lands
on the closest selectable point (vertex, circular edge center, face center).
A circular planar face has exactly ONE vertex (OCC's seam), which is why
"nearest vertex" logic used to land bolt holes R-r off axis with residual 0.
The result reports `landing` (the resolved sub-elements), `landing_points`
(where the JCS actually landed, mm) and `warnings` when a hint/anchor/point
ref snapped somewhere more than 0.5 mm from the point it aimed at.
For a cylindrical face a plain face ref lands on the AXIS (FreeCAD projects
the face center onto it) — an axis-based joint on two cylindrical faces is
the axle-in-hole form; for two rotational faces `residual_mm` is measured
AXIS-to-AXIS and the result says `residual_basis: "axis"` (a surface distance
for an axle fit is its radial gap, so a 5 mm pin in a 20 mm bore would read
as a 15 mm error even when the axes are perfect). An axis joint aligns the two
reference POINTS, so a partial curved face can slide the part along the axis:
when it moves more than 2 mm, `axial_slide_mm` reports the displacement and a
warning says to pin the axial position with `point_on_face`.

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
A mate whose residual exceeds tolerance fails. stop_on_error=True stops at the
first failure: mates that ALREADY passed stay applied (the transaction commits
when at least one passed, mirroring batch), and nothing moves only when the
FIRST mate is the one that fails. stop_on_error=False keeps going and commits
the passing mates either way.
For PERSISTENT joints use assembly_session instead.""",
    "step_plan": """\
step_plan — submit a plan to FreeCAD's step journal WITHOUT executing it.

Each entry is a cad() argument dict (or an execute_code snippet — see below),
so the panel can run it later:
    {"operation": "create_object", "obj_type": "Part::Box", "obj_name": "Base",
     "description": "base plate"}
    {"operation": "pad", "obj_name": "Sketch", "obj_properties": {"Length": 20}}

The plan lands in the document's journal as `planned` steps and shows up in
the CADPilot Steps panel; `description` becomes the panel's plan title, and a
per-step `description` becomes its row label — write both for the human
watching the panel (see operation_help("step_labels") for how to word them).
Nothing touches the model until a step is released — by
the user clicking Next in the panel, or via step_control. A new cad() call
discards whatever part of the plan has not run yet, because it was planned
against a document state that no longer exists.

Batch steps work too: {"operation": "batch", "ops": [...]} runs the ops in one
transaction as one step.

An execute_code step is plannable when it carries the code it will run
({"operation": "execute_code", "code": "import FreeCAD; …"}); a planned
snippet without `code` is refused, because it could never run. A planned
snippet runs through the same executor as the execute_code tool, in its own
transaction, and can be re-run/replayed like any other step.""",
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
                full dict), params.label renames the row. params.label alone
                also renames a DONE step (a label is presentation, not
                history: it is the only way to fix a wrong row without
                re-running the step)
  reexecute     roll back to just before `index`, then run it with `params`
                merged in (params.label renames the step). The planned tail
                survives (reject drops it), but the DONE steps after `index`
                are rolled back to planned and must be released again — the
                reply says so in `rewound` and `warning`, and a re-run needs a
                clean base

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
  reset         forget the whole journal (pass confirm=true). It only empties
                the log: it never touches the document, the undo stack or any
                object's visibility
  status        full records incl. params + meta (plan description).
                params.summary=true returns one compact row per step
                (index/state/operation/label/result, no params) instead —
                use it to see where the session stands without paying for
                every recorded snippet; params.limit/offset page the full
                records

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
  "reported after"           — a call gave up early on user back-pressure
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

dismiss=true adds the one ACTION this tool can take: it closes the modal dialog
holding the GUI queue back (Cancel semantics, so nothing on screen is ever
confirmed). That call is not held back by the dialog, which is why it works
where every other tool times out; it is part of diagnose rather than a tool of
its own because the blocker IS what this report describes.

A listening port that does not answer ping is the one case that is not a
setup problem: FreeCAD is up but its GUI thread is busy or wedged (modal
dialog, long recompute, deadlock). The addon log stays readable exactly
there — grep it with get_addon_log before restarting.

One "busy" case is deliberate, not a fault: while the user holds a mouse
button in the FreeCAD window (or a popup/modal is open) the queue is held
back, because acting on the document mid-interaction is what feels like a
freeze. A call that lands in that window now answers within a couple of
seconds with "could not act on the document within 2s because the user is
holding a mouse button… release it and retry" instead of waiting out its
whole timeout in silence. Release the button (or close the dialog) and
issue the call again; nothing is stuck.""",
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
at all.

When a capture fails, get_view returns the REASON the addon recorded, not a
guess: "the active view is 'SpreadsheetView', which has no saveImage", "the
PNG was written but could not be read back", a GUI-dispatch timeout (see
the mouse-guard note under operation_help("diagnose")), and so on. An
occluded or minimized FreeCAD window, or a model that changed in the same
breath, is the common non-view-type case: pass focus_object=<object name>.
Framing an object skips the automatic fit and forces a repaint before
saveImage, which is what turns a stale frame into a capture.""",
    "step_labels": """\
step_labels: what the Steps panel's "Step" column says, and how to write it.

The CADPilot Steps panel is read by a human reviewing the model step by step,
so every step carries ONE line that states its intent. Two sources fill it:

1. Your `description` argument — the intent, and what a reviewer needs.
   cad(operation="pad", obj_name="FlangeProfile",
       description="flange body, 6mm thick")
   - One line, <= ~40 characters, a noun phrase or short imperative, in the
     language the user is writing in.
   - Name the DESIGN DECISION, not the call: "flange body, 6mm" tells the
     reviewer what this step is for; "pad on FlangeProfile" only restates the
     operation, which the panel's Op column already shows.
   - Only the first line is used as the row label; the tooltip keeps the rest.
   - Same convention inside step_plan: a per-step `description`, plus the
     plan-level `description` as its title. An execute_code snippet states it
     as its leading `# comment` block.

   An execute_code step IS one label everywhere, built the same way in the
   panel, in step_control(status) and in the MCP reply: the snippet's leading
   `# comment` first line, then what the code did —
     "# 琴身轮廓\nimport FreeCAD…"  ->  "琴身轮廓 · read-only"
   (an older journal, or a snippet with no comment, shows the effect alone as
   "execute_code: read-only"). Editing the comment in the panel's detail pane
   and pressing Re-run updates the row.

2. The derived label, when no description was written. Every op then reads as
     <verb> '<target>' <detail>
   where <detail> is the one parameter that identifies the step:
     pad 'FlangeProfile' 6mm        pocket 'BoreProfile' 20mm
     fillet 'PolarPattern' 2mm      pattern 'BoltHoleCut' polar ×8
     sketch 'FlangeProfile' 12 geom / 24 con
     variables 'Vars' 5 cell(s)     boolean 'Body' cut
     revolution 'Profile' 360°      mirror 'Half' across XZ
     move 'Cover' Δ(0, 0, 12)       batch ×5: pad, pocket, fillet, …
   Accurate but mechanical: it says what ran, never why. Write the
   `description` whenever a reviewer would care why.""",
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
            "step_labels",
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
    "step_labels": "how to write a step's one-line description (Steps panel)",
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
