"""Addon feature-op guards found by complex-model stress testing.

Three failure modes a real PartDesign model exposed, all of which used to be
SILENT (the tool reported success while the geometry was wrong):

* a sketch attached to a face by NAME lands on the wrong plane after the next
  feature re-derives face names — its pocket then cuts air;
* a PartDesign ``pattern`` arrayed the whole body (Draft's array copies the
  base Shape) or repeated nothing, so "6 holes" came out as 6 overlapping
  plates or as 1 hole;
* a cut that removed nothing reported plain success.

The addon cannot be imported without FreeCAD, so these parse the source.
"""

import ast
from pathlib import Path

_ADDON = Path(__file__).resolve().parents[1] / "addon" / "CADPilot" / "rpc_server"
_FEATURE = ast.parse((_ADDON / "feature_ops.py").read_text(encoding="utf-8"))
_SKETCHER = ast.parse((_ADDON / "sketcher_ops.py").read_text(encoding="utf-8"))


def _func(tree, name):
    return next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )


def _strings(node) -> set[str]:
    return {
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def test_face_attachment_accepts_a_direction_selector():
    body = _func(_SKETCHER, "_attach_sketch")
    assert "_resolve_semantic_face" in _strings(body) or any(
        isinstance(n, ast.Name) and n.id == "_resolve_semantic_face" for n in ast.walk(body)
    ), "plane.face must accept a direction token"


def test_semantic_face_resolution_uses_normals_and_extremity():
    # The direction words live in the module-level _FACE_WORDS table.
    words = _strings(_SKETCHER)
    for token in ("+X", "-X", "+Y", "-Y", "+Z", "-Z", "top", "bottom", "right", "left"):
        assert token in words, f"{token} is not handled"
    body = _func(_SKETCHER, "_resolve_semantic_face")
    # Resolution compares the face normal against the wanted axis...
    assert any(n.attr == "normalAt" for n in ast.walk(body) if isinstance(n, ast.Attribute))
    # ...and breaks ties by how far the face sits along it.
    assert any(n.attr == "CenterOfMass" for n in ast.walk(body) if isinstance(n, ast.Attribute))


def test_partdesign_pattern_uses_a_partdesign_pattern_not_a_draft_array():
    body = _func(_FEATURE, "_build_pd_pattern")
    assert "PartDesign::PolarPattern" in _strings(body)
    assert "PartDesign::LinearPattern" in _strings(body)
    # Originals must point at the FEATURE, not at the whole body's shape.
    assert any(isinstance(n, ast.Attribute) and n.attr == "Originals" for n in ast.walk(body))


def test_partdesign_pattern_refuses_to_return_a_no_op():
    """Silently returning one hole where six were asked for is worse than an
    error — the builder must compare volumes and raise."""
    body = _func(_FEATURE, "_build_pd_pattern")
    raises = [n for n in ast.walk(body) if isinstance(n, ast.Raise)]
    assert raises, "a no-effect pattern must raise"
    assert any("had no effect" in s for s in _strings(body))


def test_cut_no_op_is_detected_against_the_predecessor():
    body = _func(_FEATURE, "_cut_removed_nothing")
    assert "BaseFeature" in _strings(body), "the baseline is the feature's predecessor"
    assert any(isinstance(n, ast.Attribute) and n.attr == "Volume" for n in ast.walk(body))
    describe = _func(_FEATURE, "describe_feature")
    assert "_CUT_TYPES" in _strings(describe) or any(
        isinstance(n, ast.Name) and n.id == "_CUT_TYPES" for n in ast.walk(describe)
    ), "describe_feature must consult the cut types"
    assert "warnings" in _strings(describe)
    assert "pocket" in _strings(_FEATURE), "_CUT_TYPES must cover pocket"
