"""Body.Tip policy — pure decision logic, no FreeCAD needed.

CADPilot relied on FreeCAD advancing ``Body.Tip`` on its own. That holds for
a pad/pocket created in a body, but NOT for a PartDesign *transform* feature:
live on 1.1.4, ``cad(operation="pattern", pattern_type="polar", count=6)`` on
a flange produced a correct ``PartDesign::PolarPattern`` (its own Shape was the
6-hole result, 15246 mm^3) while ``Body.Tip`` stayed on the single-hole pocket
and the Body's volume stayed 15874 mm^3 — one hole. The tool reported success.
Assigning ``body.Tip = pattern`` fixed it immediately.

So the rule lives here, in a module with no FreeCAD import, and the feature
builder consults it instead of trusting the ambient tip.
"""

import sys
from pathlib import Path

import pytest

_ADDON = Path(__file__).resolve().parents[1] / "addon" / "CADPilot"
if str(_ADDON) not in sys.path:
    sys.path.insert(0, str(_ADDON))

from rpc_server import tip_policy as tp  # noqa: E402


def test_transform_features_must_be_pushed_to_the_tip():
    """The live bug: a pattern in a body stayed inert because Tip never moved."""
    assert tp.advances_tip("PartDesign::PolarPattern")
    assert tp.advances_tip("PartDesign::LinearPattern")
    assert tp.advances_tip("PartDesign::MultiTransform")


def test_additive_subtractive_and_dressup_features_advance_the_tip():
    for type_id in (
        "PartDesign::Pad",
        "PartDesign::Pocket",
        "PartDesign::Revolution",
        "PartDesign::Groove",
        "PartDesign::Thickness",
        "PartDesign::Draft",
        "PartDesign::Fillet",
        "PartDesign::Chamfer",
        "PartDesign::Boolean",
    ):
        assert tp.advances_tip(type_id), type_id


def test_sketches_datums_and_binders_never_become_the_tip():
    """A Tip must be a modelling feature; a sketch or a datum is not."""
    for type_id in (
        "Sketcher::SketchObject",
        "PartDesign::Body",
        "PartDesign::Plane",
        "PartDesign::Line",
        "PartDesign::Point",
        "PartDesign::CoordinateSystem",
        "PartDesign::ShapeBinder",
        "PartDesign::SubShapeBinder",
        "App::FeaturePython",  # the static hull result
        "Part::Fillet",  # a document-root object, belongs to no body
        "PartDesign::",
        "",
    ):
        assert not tp.advances_tip(type_id), type_id


def test_dressup_is_a_partdesign_feature_only_inside_a_body():
    """#2: a Part::Fillet at the document root is not part of the body — its
    Placement does not follow the body and assigning it to Body.Tip leaves the
    body Invalid. Inside a body the dress-up must be the PartDesign feature."""
    assert tp.dress_type("fillet", base_in_body=True) == "PartDesign::Fillet"
    assert tp.dress_type("fillet", base_in_body=False) == "Part::Fillet"
    assert tp.dress_type("chamfer", base_in_body=True) == "PartDesign::Chamfer"
    assert tp.dress_type("chamfer", base_in_body=False) == "Part::Chamfer"


def test_dressup_size_property_names():
    """FreeCAD 1.1 keeps a scalar size on each dress-up feature (the per-edge
    tuple form is Part-level only)."""
    assert tp.dress_size_property("fillet") == "Radius"
    assert tp.dress_size_property("chamfer") == "Size"


def test_dressup_spec_keys_stay_radius_and_size():
    """The cad() spec key ('radius'/'size') is not the operation name
    ('fillet'/'chamfer'). Conflating them made every fillet op fail with
    "fillet requires: fillet" — caught live, not by the source-parsing tests."""
    assert tp.dress_spec_key("fillet") == "radius"
    assert tp.dress_spec_key("chamfer") == "size"


def test_dressup_is_allowed_on_a_body_tip_or_a_part_level_base():
    assert tp.dress_base_is_allowed(base_in_body=False, base_is_tip=False)  # Part-level build
    assert tp.dress_base_is_allowed(base_in_body=True, base_is_tip=True)


def test_dressup_on_a_mid_chain_feature_is_refused():
    """Measured on 1.1.4: fillet the Pad while a pattern sits after it, and
    FreeCAD moves Body.Tip onto the fillet — the body then shows 15999 mm^3
    with the six holes GONE. PartDesign has no safe API for inserting a dress-up
    mid-chain (Body.insertObject duplicated the Group entry and left the tip
    wrong), so the op must refuse instead of silently discarding later features.
    """
    assert not tp.dress_base_is_allowed(base_in_body=True, base_is_tip=False)


def test_unknown_dressup_kind_raises():
    with pytest.raises(ValueError, match="dress"):
        tp.dress_type("bevel", base_in_body=True)
    with pytest.raises(ValueError, match="dress"):
        tp.dress_size_property("bevel")


def test_tip_moves_to_a_successor_of_the_current_tip():
    """The pattern case: its Originals point at the pocket that IS the tip."""
    assert tp.should_advance(
        "PartDesign::PolarPattern",
        body_has_tip=True,
        tip_is_feat=False,
        feat_depends_on_tip=True,
    )


def test_tip_does_not_move_to_an_earlier_feature():
    """Dressing up a feature in the MIDDLE of the chain must not become the tip:
    the body's tip is what the body shows, so claiming it would silently hide
    every later feature (the pattern's holes) — the same class of silent wrong
    geometry this module exists to stop."""
    assert not tp.should_advance(
        "PartDesign::Fillet",
        body_has_tip=True,
        tip_is_feat=False,
        feat_depends_on_tip=False,  # fillets the pad; the pattern sits after it
    )


def test_tip_is_taken_in_a_fresh_body():
    assert tp.should_advance(
        "PartDesign::Pad", body_has_tip=False, tip_is_feat=False, feat_depends_on_tip=False
    )


def test_tip_already_pointing_at_the_feature_is_a_no_op():
    assert not tp.should_advance(
        "PartDesign::Pad", body_has_tip=True, tip_is_feat=True, feat_depends_on_tip=True
    )


def test_non_feature_types_never_take_the_tip():
    assert not tp.should_advance(
        "Sketcher::SketchObject",
        body_has_tip=True,
        tip_is_feat=False,
        feat_depends_on_tip=True,
    )
