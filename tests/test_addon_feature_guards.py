"""Addon feature-op guards found by complex-model stress testing.

Three failure modes a real PartDesign model exposed, all of which used to be
SILENT (the tool reported success while the geometry was wrong):

* a sketch attached to a face by NAME lands on the wrong plane after the next
  feature re-derives face names — its pocket then cuts air;
* a PartDesign ``pattern`` arrayed the whole body (Draft's array copies the
  base Shape) or repeated nothing, so "6 holes" came out as 6 overlapping
  plates or as 1 hole;
* a cut that removed nothing reported plain success.

Plus, found by re-verifying a user bug report on 1.1.4:

* a PartDesign *transform* feature was created correctly but never became its
  Body's Tip, so the body kept showing the pre-pattern result while the tool
  reported success (``body.Tip`` is now pushed explicitly — see tip_policy);
* a fillet/chamfer on a base inside a Body was built as a document-root
  ``Part::Fillet``, which is not part of the Body at all.

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


def test_move_accepts_list_vectors():
    """The whole API takes plain coordinate lists; move's dict-only form used
    to crash with "'list' object has no attribute 'get'" on the natural input."""
    body = _func(_FEATURE, "_build_move")
    assert any(isinstance(n, ast.Name) and n.id == "_move_vec3" for n in ast.walk(body)), (
        "translate/rotate/placement vectors must go through a list-tolerant parser"
    )


def test_move_vec3_helper_parses_lists_and_dicts():
    helper = _func(_FEATURE, "_move_vec3")
    seg = ast.unparse(helper)
    assert "list" in seg and "tuple" in seg, "lists must be accepted"
    assert "dict" in seg, "dicts must stay accepted"
    assert "raise ValueError" in seg, "malformed vectors must raise a clear error"


def test_datum_plane_accepts_a_direction_face_token():
    """datum_plane's plane.face used to stuff the token straight into the
    attachment — '+Z' is no subelement, so it recomputed Invalid. It must use
    the same direction resolution as a sketch's plane.face."""
    body = _func(_FEATURE, "_build_datum_plane")
    assert any(
        isinstance(n, ast.Attribute) and n.attr == "_resolve_semantic_face" for n in ast.walk(body)
    ), "datum_plane plane.face must resolve direction tokens"
    assert any(isinstance(n, ast.Attribute) and n.attr == "_FACE_WORDS" for n in ast.walk(body)), (
        "direction words must be recognized"
    )


def test_recompute_failure_reports_status_string():
    """'failed to recompute (check parameters/geometry)' alone sends the caller
    hunting blind; FreeCAD's StatusString carries the actual reason."""
    body = _func(_FEATURE, "create_feature_gui")
    assert any(isinstance(n, ast.Constant) and n.value == "StatusString" for n in ast.walk(body)), (
        "read the feature's StatusString for the failure detail"
    )


def _names(node) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def test_new_feature_is_pushed_as_its_body_tip():
    """Measured on 1.1.4: a polar pattern built from a flange pocket was correct
    (its own Shape was the 6-hole result) yet Body.Tip stayed on the pocket, so
    the body kept ONE hole while the op returned success. Trusting FreeCAD to
    advance the tip is not enough for transform features."""
    helper = _func(_FEATURE, "_advance_body_tip")
    assert "tip_policy" in _names(helper), "the tip decision belongs in tip_policy"
    assert any(isinstance(n, ast.Attribute) and n.attr == "Tip" for n in ast.walk(helper)), (
        "the helper must assign body.Tip"
    )
    assert "advances_tip" in {n.attr for n in ast.walk(helper) if isinstance(n, ast.Attribute)}, (
        "consult the whitelist before assigning a tip"
    )
    assert "should_advance" in {n.attr for n in ast.walk(helper) if isinstance(n, ast.Attribute)}, (
        "only a successor of the current tip may claim it, or a later feature is hidden"
    )
    # And the single creation entry point must actually call it.
    assert "_advance_body_tip" in _names(_func(_FEATURE, "create_feature_gui")), (
        "create_feature_gui must push the tip after building the feature"
    )


def test_tip_advance_tolerates_a_body_less_feature():
    """A Part-level base (or a document-root build) has no body: the helper must
    return quietly instead of raising — the guard already ran the whole op inside
    a transaction and an exception would discard a valid feature."""
    helper = _func(_FEATURE, "_advance_body_tip")
    assert any(isinstance(n, ast.Return) for n in ast.walk(helper))
    assert "None" in _strings(helper) or any(
        isinstance(n, ast.Constant) and n.value is None for n in ast.walk(helper)
    ), "a missing body must be a plain early return"


def test_dressup_inside_a_body_builds_the_partdesign_feature():
    """#2: Part::Fillet is a document-root object — not in Body.Group, it does
    not follow the Body's Placement, and Body.Tip = <it> is accepted silently
    while leaving the Body ['Touched', 'Invalid']."""
    body = _func(_FEATURE, "_build_fillet_chamfer")
    assert "tip_policy" in _names(body), "the Part-vs-PartDesign choice belongs in tip_policy"
    assert "newObject" in {n.attr for n in ast.walk(body) if isinstance(n, ast.Attribute)}, (
        "the PartDesign dress-up must be created inside the Body"
    )
    assert "dress_type" in {n.attr for n in ast.walk(body) if isinstance(n, ast.Attribute)}
    assert "dress_size_property" in {
        n.attr for n in ast.walk(body) if isinstance(n, ast.Attribute)
    }, "PartDesign::Fillet takes a scalar Radius, not the Part-level Edges tuples"


def test_invalid_shape_error_names_the_object_and_reason():
    """Reported live: 'shape is invalid' was raised for a self-intersecting
    meridian, a broken reference and a corrupted dependency graph alike — three
    different causes, one opaque message. It must name the object and carry
    FreeCAD's own reason."""
    helper = _func(_FEATURE, "_invalid_shape_error")
    text = ast.unparse(helper)
    assert "produced an invalid Shape" in text
    assert ".Name" in text, "the object name is what makes the error actionable"
    assert "StatusString" in text, "FreeCAD's reason must be carried through"
    assert "_invalid_shape_error" in _names(_func(_FEATURE, "create_feature_gui"))
