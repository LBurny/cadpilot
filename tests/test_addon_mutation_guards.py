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
