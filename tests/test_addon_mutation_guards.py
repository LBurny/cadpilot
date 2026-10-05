"""Addon guards found by a parallel multi-agent modeling stress run.

Five failures that a real session with several documents open exposed:

* Part-level ``pattern`` (the Draft path) died with "PropertyLink does not
  support external object" whenever the target document was open but not
  ACTIVE — which is the norm once more than one document is open;
* ``delete_object`` on a PartDesign Body member left ``Body.Tip`` dangling, so
  the Body reported "shape is invalid" and every later feature on it failed;
* the auto ``faceN_center`` anchor used the face's AREA centroid, which for an
  annular face (a bushing/washer/flange top) sits in the hole — an
  anchor-based mate then reported "no planar face within 1 mm";
* ``pattern``'s count rejected "=expr", so a Spreadsheet could not drive an
  array while every other op accepts expressions;
* ``loft`` ignored obj_name, naming every loft "Loft" while operation_help
  documents obj_name as the new loft's name.

The addon cannot be imported without FreeCAD, so these parse the source.
"""

import ast
from pathlib import Path

_ADDON = Path(__file__).resolve().parents[1] / "addon" / "CADPilot" / "rpc_server"


def _tree(name):
    return ast.parse((_ADDON / name).read_text(encoding="utf-8"))


def _func(tree, name):
    return next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )


def _names(node) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _attrs(node) -> set[str]:
    return {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}


FEATURE = _tree("feature_ops.py")
FACTORY = _tree("object_factory.py")
ASSEMBLY = _tree("assembly_ops.py")
RPC = _tree("rpc_server.py")
STEP_ENGINE = _tree("step_engine.py")
JOINTS = _tree("joint_ops.py")
GEOMETRY = _tree("geometry_query.py")
SKETCHER = _tree("sketcher_ops.py")


def test_global_frame_covers_every_shape_query():
    """get_topology / get_positioning_info kept reading obj.Shape directly after
    the frame fix landed in measure/interference — a body member then listed
    LOCAL coordinates as if they were global, which is the "move a Body, then
    query its Pad" workflow (live-verified after the first pass)."""
    assert "global_shape" in _names(_func(GEOMETRY, "get_topology"))
    positioning = _func(GEOMETRY, "get_positioning_info")
    assert "global_shape" in _names(positioning)
    assert "global_placement" in _names(positioning)


def test_cut_warning_looks_for_material_beyond_the_cut_itself():
    """By the time the warning runs, the body's tip IS the cut feature, so its
    own extrusion looked like material and the warning went silent
    (live-verified: an un-attached sketch in an empty body reported plain
    success with a floating disk)."""
    helper = _func(FEATURE, "_cut_without_material")
    assert "Group" in _strings_in(helper), "material must come from another member"
    src = ast.unparse(helper)
    assert "member is feat" in src, "the cut's own result must not count as material"
    assert "BaseFeature" in _strings_in(helper), "an adopted base solid counts"
    assert "_cut_without_material" in _names(_func(FEATURE, "describe_feature"))


def test_datum_plane_echo_unwraps_link_sub_subelements():
    """LinkSub sub-elements arrive as lists/tuples: str() on them leaked a
    Python repr ("('Face3',)") and the face echo silently lost center/normal."""
    src = ast.unparse(_func(SKETCHER, "_attach_sketch"))
    assert "isinstance(subs, (list, tuple))" in src
    assert "datum_plane" in _strings_in(_func(FEATURE, "describe_feature"))


def test_mate_reports_no_moved_link_when_nothing_moved():
    """A mate the solver satisfied without displacement must not name a link:
    the old fallback reported the untouched ground link with pre == moved_to,
    and the MCP recorder then 'restored' a part that never left."""
    src = ast.unparse(_func(JOINTS, "_op_mate"))
    assert "satisfied without moving any component" in src
    assert "primary = None" in src


def test_move_redirects_partdesign_features_and_verifies_persistence():
    """A PartDesign feature's Placement is rewritten by the next recompute, so
    moving one reported success while the geometry stayed (live: a moved Pad was
    back at (0,0,0) on the next cad() call and a following boolean fused the
    un-moved shape). The move must go to the owning Body and prove it stuck."""
    move = _func(FEATURE, "_build_move")
    assert "body_owner" in _names(move), "a feature must be redirected to its Body"
    assert "_assign_placement" in _names(move)
    assign = _func(FEATURE, "_assign_placement")
    assert "recompute" in _attrs(assign) or "recompute" in {
        n.attr for n in ast.walk(assign) if isinstance(n, ast.Attribute)
    }, "persistence can only be proven after a recompute"
    assert any("did not persist" in s for s in _strings_in(assign)), (
        "a move FreeCAD undoes must fail loudly, not report success"
    )


def _strings_in(node) -> set:
    return {
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def test_thickness_rejects_all_faces_and_warns_on_a_noop():
    """faces='all' opens every wall: FreeCAD returns the solid unchanged while
    the op reported success with identical geometry (live). The selector must be
    refused, and a shell that changed nothing must warn."""
    body = _func(FEATURE, "_build_thickness")
    assert any("faces='all' removes every wall" in s for s in _strings_in(body))
    describe = _func(FEATURE, "describe_feature")
    assert "_thickness_changed_nothing" in _names(describe)


def test_padlike_ops_adopt_an_attached_material_base():
    """A pocket whose sketch hangs off a datum plane had NO material to cut and
    extruded its profile as a floating disk (live: 942.5 mm^3 of nothing).
    pad/pocket/revolution/groove must resolve the attachment chain and adopt the
    solid the way the GUI's 'Base feature' does."""
    for builder in ("_build_padlike", "_build_revlike"):
        assert "_ensure_material_base" in _names(_func(FEATURE, builder)), builder
    helper = _func(FEATURE, "_ensure_material_base")
    assert "BaseFeature" in _attrs(helper)
    walker = _func(FEATURE, "_padlike_attachment_base")
    assert "AttachmentSupport" in _strings_in(walker), (
        "the chain is walked through AttachmentSupport (sketch -> datum -> solid)"
    )


def test_mate_records_the_links_the_solver_actually_moved():
    """The solver picks the side it moves; naming ref_a's link restored a part
    that never moved and left the moved one displaced (live: a hinge lid stayed
    at 70..90 after rollback instead of returning to 150..170)."""
    mate = _func(JOINTS, "_op_mate")
    assert "_links_of" in _names(mate), "snapshot every component link"
    assert "_same_placement" in _names(mate), "diff before/after the solve"
    assert "moved_links" in _strings_in(mate), "report the full set for the MCP recorder"
    assert "_duplicate_joint_warnings" in _names(mate), (
        "a second joint on the same pair must not be silently accepted"
    )
    rollback = _func(JOINTS, "_op_rollback_step")
    assert "remove_assembly" in _strings_in(rollback), (
        "rolling back across 'start' must tear the assembly down"
    )


def test_geometry_queries_globalize_body_member_frames():
    """A PartDesign feature's Shape is in the Body's LOCAL frame while the Body
    owns the Placement: querying a feature of a moved Body returned local
    coordinates as if they were global, so measure/interference/anchors
    silently mis-placed the part (live-verified)."""
    for fn in ("measure_geometry", "_get_shape", "_element_spatial"):
        assert "global_shape" in _names(_func(GEOMETRY, fn)), (
            f"{fn} must read shapes through global_shape"
        )
    placement_helper = _func(GEOMETRY, "global_placement")
    assert "getGlobalPlacement" in _attrs(placement_helper)
    for fn in ("_auto_anchor_map", "_to_global"):
        assert "global_shape" in _names(_func(ASSEMBLY, fn)) or "global_placement" in _names(
            _func(ASSEMBLY, fn)
        ), f"anchors must live in the object's real frame ({fn})"


def test_draft_pattern_activates_the_target_document():
    """Draft.make_array creates its Array in FreeCAD.ActiveDocument, so the
    base object's document has to be active for the call."""
    assert "_active_document" in _names(_func(FEATURE, "_build_pattern"))
    helper = _func(FEATURE, "_active_document")
    assert "setActiveDocument" in _attrs(helper)
    # The user's view must be handed back afterwards.
    assert any(isinstance(n, ast.Try) and n.finalbody for n in ast.walk(helper)), (
        "the previous active document must be restored in a finally block"
    )


def test_pattern_count_accepts_an_expression():
    assert "_pattern_count" in _names(_func(FEATURE, "_build_pattern"))
    helper = _func(FEATURE, "_pattern_count")
    assert "setExpression" in _attrs(helper), (
        "count='=Vars.n_holes' must go through the expression engine, not int()"
    )
    assert "FeaturePython" in ast.unparse(helper), (
        "the value is evaluated up front, before the feature exists"
    )


def test_loft_honours_obj_name():
    """A base-less op receives obj_name as spec['base']; reading only
    spec['name'] named every loft 'Loft'."""
    body = _func(FEATURE, "_build_loft")
    strings = {
        n.value for n in ast.walk(body) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert "base" in strings, 'the new loft\'s name is spec["base"] for a base-less op'


def test_delete_repairs_a_body_tip():
    helper = _func(FACTORY, "repair_body_tips")
    assert "Tip" in _attrs(helper), "a deleted tip leaves the Body invalid"
    assert "tip_policy" in _names(helper) and "advances_tip" in _attrs(helper), (
        "only a solid-producing member may become the tip: a sketch as tip would "
        "stop the next feature from claiming it and hide it"
    )
    assert "repair_body_tips" in _names(_func(FACTORY, "delete_object_gui")), (
        "delete_object_gui must repair the tip after removing the object"
    )
    # The same repair must run where other removals happen: the batch's failed
    # sub-op cleanup and the journal's rollback cleanup both delete objects that
    # can be a Body's tip (live: a failed thickness took the Tip with it and
    # every later dress-up on that Body was refused).
    assert "repair_body_tips" in _names(_func(RPC, "_run_batch"))
    assert "repair_body_tips" in _names(_func(STEP_ENGINE, "_remove_objects_locked"))


def test_face_center_anchor_lies_on_the_face():
    assert "_face_center_on_surface" in _names(_func(ASSEMBLY, "_auto_anchor_map"))
    helper = _func(ASSEMBLY, "_face_center_on_surface")
    assert "distToShape" in _attrs(helper), (
        "only fall back to an on-face point when the centroid is off the face"
    )
    assert "CenterOfMass" in _attrs(helper), "a regular face must keep its true center"


def test_base_planes_match_freecads_own_origin_planes():
    """plane="XZ" and a datum plane on XZ_Plane must mean the SAME frame.

    The table used to hold R_x(-90) for XZ (FreeCAD's XZ_Plane is R_x(+90))
    and R_y(90) for YZ (FreeCAD: R_z(90)xR_x(90)): the same mug profile drawn
    on plane="XZ" came out upside down / below the XY plane while
    datum_plane("XZ") + plane={"datum": ...} produced it upright — two
    documented spellings of "XZ" with mirrored results (live: a 90-degree
    revolve landed at z -90..0; the profile's +v must map to global +Z).
    """
    table = {}
    for n in SKETCHER.body:
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "_BASE_PLANES":
            table = ast.literal_eval(n.value)
    assert table == {"XY": (0, 0, 0), "XZ": (0, 0, 90), "YZ": (90, 0, 90)}, (
        f"origin-plane yaw/pitch/roll expected, got {table}"
    )
    attach = _func(SKETCHER, "_attach_sketch")
    src = ast.unparse(attach)
    assert "_BASE_PLANES[key]" in src and "FreeCAD.Rotation(*" in src, (
        "the rotation must come from the table"
    )
    echo = _strings_in(attach)
    assert {"x_axis", "y_axis", "normal"} <= echo, (
        "a base-plane sketch echoes its local axes in global terms"
    )


def test_revolution_refuses_the_profile_normal_and_defaults_to_an_in_plane_axis():
    """axis="Z" is the profile's own normal: revolving a planar profile about
    it keeps every point in the drawing plane, so the result is a flat
    zero-volume shell that FreeCAD still calls valid (live: volume -0.0, bbox
    +-98 for a 40x90 profile, reported as plain success). The old default was
    that same "Z", so a bare revolution could never produce a solid."""
    builder = _func(FEATURE, "_build_revlike")
    src = ast.unparse(builder)
    assert "spec.get('axis', 'Y')" in src, "the default must be an in-plane axis"
    assert "N_Axis" in src and "zero-volume shell" in src, (
        "the normal axis is refused with the reason"
    )
    create = ast.unparse(_func(FEATURE, "create_feature_gui"))
    assert "-1e-06" in create and "self-intersecting" in create, (
        "a negative-volume result (profile crossing its axis) must fail the op"
    )


def test_the_spatial_audit_inflates_its_prefilter_boxes():
    """OCC's BoundBox comes from control points and can UNDER-report a curved
    shape (a tube-radius-8 torus reports z +-7.94), so a prefilter built on it
    skips pairs that really touch — a false island — and inflates nearest
    distances into false "floating" reports."""
    helper = _func(ASSEMBLY, "_audit_bbox")
    assert "enlarge" in _attrs(helper), "the box must be enlarged before filtering"
    verify = ast.unparse(_func(ASSEMBLY, "verify_assembly"))
    assert "bboxes" in verify and verify.count("_audit_bbox(") >= 1
    assert "BoundBox" not in verify.replace("_audit_bbox", ""), (
        "no prefilter may use the raw BoundBox"
    )


def test_read_only_reports_carry_six_significant_digits():
    """Callers diff two reports to get a delta (a 0.05 mm wall change on a
    240 mm part): at 4 digits the rounding of the operands is the same order
    as the answer."""
    src = ast.unparse(_func(GEOMETRY, "_r"))
    assert ".6g" in src, "6 significant digits"


def test_resolved_face_echo_lists_same_direction_candidates():
    """On a stepped part ("+Z" on a phone stand) the picked face can be right
    while a different same-direction face was meant; the echo must show the
    alternatives instead of forcing trial and error."""
    src = ast.unparse(_func(FEATURE, "_record_resolved_faces"))
    assert "same_direction" in src and "dot(" in src and "isSame" in src


def test_a_bare_recompute_failure_names_the_ops_own_trap():
    """thickness on a face with an inner wire fails with an EMPTY StatusString
    (measured live: a plate with a blind pocket refused "+Z" while "-Z"
    worked), so "check parameters/geometry" sent the caller hunting blind."""
    src = ast.unparse(_func(FEATURE, "create_feature_gui"))
    assert "_FAILURE_HINTS" in src, "the failure path must consult the per-op hints"
    hints = {}
    for n in FEATURE.body:
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "_FAILURE_HINTS":
            hints = ast.literal_eval(n.value)
    assert "thickness" in hints and "inner wire" in hints["thickness"]


def test_a_body_members_own_placement_is_never_applied_twice():
    """FreeCAD computes a PartDesign feature's Shape in its Body's frame with
    the sketch's plane attachment already baked in, so the feature's own
    Placement must NOT be composed on top: getGlobalPlacement() multiplied it
    in and rotated XZ/YZ/face-attached features twice (live: an XZ pad read
    y -20..0 / z -5..0 for a body at the origin, and check_interference
    reported a 500 mm^3 overlap as "no intersection"; Part.getShape() and the
    Body agreed with .Shape)."""
    helper = _func(GEOMETRY, "global_placement")
    src = ast.unparse(helper)
    assert "body.getGlobalPlacement()" in src, "the BODY's frame, not the feature's own"
    calls_on_obj = [
        n
        for n in ast.walk(helper)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "getGlobalPlacement"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "obj"
    ]
    assert not calls_on_obj, "never compose the member's own placement (the docstring may name it)"
    assert "global_placement" in _names(_func(GEOMETRY, "global_shape"))


def test_the_audit_measures_a_floating_distance_exactly():
    """The bbox prefilter box is INFLATED by 0.25 mm, so falling back to its
    distance under-read a gap by up to 0.5 mm (live: a true 3.0 mm gap
    reported as 2.5). When no pair is inside the scan range, pay one exact
    distToShape to the nearest candidate."""
    src = ast.unparse(_func(ASSEMBLY, "verify_assembly"))
    assert "distToShape(dict(shaped)[best_name])" in src, "exact fallback before reporting"


def test_a_negative_volume_is_only_judged_on_a_shape_holding_solids():
    """A PartDesign::Plane's infinite face reports a garbage volume whose SIGN
    follows its offset, so an unscoped negative-volume check refused every
    datum_plane with a negative offset (live: offset -5 → "volume -1.3e+98")."""
    src = ast.unparse(_func(FEATURE, "create_feature_gui"))
    assert "Solids" in src and "volume < -1e-06" in src, "scope the guard to solids"


def test_multi_tool_booleans_use_the_multi_forms():
    """A compound Tool does not union OVERLAPPING tools: a box+arm+knuckle fuse
    measured 17461.9 mm^3 against the true 15765.5 (Part::MultiFuse), and for
    common a compound computes base ∩ (T1 ∪ T2) instead of base ∩ T1 ∩ T2
    (800 vs 0 mm^3 on the same geometry) — silent wrong geometry either way."""
    src = ast.unparse(_func(FEATURE, "_build_boolean"))
    assert "Part::MultiFuse" in src, "fuse/cut aggregate with a real union"
    assert "Part::MultiCommon" in src, "common intersects ALL inputs"
    assert "Part::Compound" not in src, "the compound Tool is the trap"


def test_axis_joints_report_sliding_along_the_axis():
    """An axis joint aligns the two reference POINTS, so a partial curved face
    (its point is the surface's own axis base) can slide the part tens of mm
    along the axis while the residual reads 0 — live: a revolute translated a
    lid -20.62 mm along X and buried the barrels in each other, unmentioned."""
    src = ast.unparse(_func(JOINTS, "_op_mate"))
    assert "axial_slide_mm" in src and "ALONG the joint axis" in src
    assert "_AXIS_JOINTS" in _names(_func(JOINTS, "_op_mate"))


def test_face_attached_sketch_echo_says_where_its_origin_is():
    """The echo reported only the FACE center, which a caller reads as "the
    sketch origin is here" — while the origin sits on the face's parametric
    origin (a corner). Live: a centered rib profile drew around the corner and
    hung half outside the part, with the volume still adding up."""
    src = ast.unparse(_func(SKETCHER, "_attach_sketch"))
    assert "sketch_origin" in src and "center" in src
    assert "parametric origin" in ast.unparse(SKETCHER), "the note explains the default"


def test_edit_object_reaches_dynamic_spreadsheet_cells_and_explains_placement():
    """Live: edit_object with {"cells": ...} on a Spreadsheet failed "cells:
    Invalid type" (cells are dynamic, not in PropertiesList — only the
    variables op could write them), and a Placement given as [x, y, z] died
    with "'list' object has no attribute 'get'".
    """
    tree = _tree("property_mapper.py")
    mapper = _func(tree, "set_object_property")
    src = ast.unparse(mapper)
    assert "Spreadsheet::Sheet" in src and "set_spreadsheet_cells" in src, (
        "cells route through the shared setter at the OUTER level (they are not in PropertiesList)"
    )
    assert "Placement as a list must be [x, y, z]" in src, "a list Placement explains itself"
    helper = _func(tree, "set_spreadsheet_cells")
    text = " ".join(sorted(_strings_in(helper)))
    assert "was rejected" in text and "Aliases must" in text, (
        "the alias error names the cell and says what an alias may contain"
    )
    # One setter, two callers: the variables op must not keep a second copy.
    assert "set_spreadsheet_cells" in _names(_func(FEATURE, "_build_variables"))
    assert "bool is not a valid value" not in ast.unparse(_func(FEATURE, "_build_variables"))
