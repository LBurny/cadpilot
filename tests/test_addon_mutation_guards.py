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
    helper = _func(FACTORY, "_repair_body_tips")
    assert "Tip" in _attrs(helper), "a deleted tip leaves the Body invalid"
    assert "tip_policy" in _names(helper) and "advances_tip" in _attrs(helper), (
        "only a solid-producing member may become the tip: a sketch as tip would "
        "stop the next feature from claiming it and hide it"
    )
    assert "_repair_body_tips" in _names(_func(FACTORY, "delete_object_gui")), (
        "delete_object_gui must repair the tip after removing the object"
    )


def test_face_center_anchor_lies_on_the_face():
    assert "_face_center_on_surface" in _names(_func(ASSEMBLY, "_auto_anchor_map"))
    helper = _func(ASSEMBLY, "_face_center_on_surface")
    assert "distToShape" in _attrs(helper), (
        "only fall back to an on-face point when the centroid is off the face"
    )
    assert "CenterOfMass" in _attrs(helper), "a regular face must keep its true center"
