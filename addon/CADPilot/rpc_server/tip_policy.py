"""Which created feature must become its PartDesign Body's Tip.

Pure decision data — no FreeCAD import, so it is unit-tested from ``tests/``.

Why this exists: CADPilot used to trust FreeCAD to advance ``Body.Tip`` by
itself. Measured live on FreeCAD 1.1.4, that trust is misplaced for PartDesign
*transform* features. ``cad(operation="pattern", pattern_type="polar",
count=6)`` on a flange built a correct ``PartDesign::PolarPattern`` — the
pattern's own Shape was the 6-hole result (15246 mm^3) — but ``Body.Tip`` was
still the single-hole pocket, so the Body's volume stayed 15874 mm^3: one hole,
reported as success. ``body.Tip = pattern`` fixed it on the spot.

So the builder asks this module instead, and the tip is pushed explicitly.
The set is a whitelist on purpose: an unknown (newer-FreeCAD) type is left
alone rather than assigned as a Tip, because a wrong Tip is far worse than a
stale one — assigning an object that is not a body member leaves the body
``Invalid`` (see ``dress_type`` below).
"""

from __future__ import annotations

# PartDesign features that are a modelling step, so the body should show them.
_ADVANCING_TYPES = frozenset(
    {
        # additive / subtractive
        "PartDesign::Pad",
        "PartDesign::Pocket",
        "PartDesign::Revolution",
        "PartDesign::Groove",
        "PartDesign::AdditivePipe",
        "PartDesign::SubtractivePipe",
        "PartDesign::AdditiveLoft",
        "PartDesign::SubtractiveLoft",
        "PartDesign::Hole",
        # dress-up
        "PartDesign::Thickness",
        "PartDesign::Draft",
        "PartDesign::Fillet",
        "PartDesign::Chamfer",
        # boolean
        "PartDesign::Boolean",
        # transforms — the ones FreeCAD does NOT advance by itself
        "PartDesign::PolarPattern",
        "PartDesign::LinearPattern",
        "PartDesign::MultiTransform",
    }
)

_DRESS = {
    "fillet": {
        "part": "Part::Fillet",
        "pd": "PartDesign::Fillet",
        "prop": "Radius",
        "spec": "radius",
    },
    "chamfer": {
        "part": "Part::Chamfer",
        "pd": "PartDesign::Chamfer",
        "prop": "Size",
        "spec": "size",
    },
}


def advances_tip(type_id: str) -> bool:
    """True when ``type_id`` must be assigned as its Body's Tip after creation."""
    return type_id in _ADVANCING_TYPES


def should_advance(
    type_id: str,
    *,
    body_has_tip: bool,
    tip_is_feat: bool,
    feat_depends_on_tip: bool,
) -> bool:
    """True when the new feature should become its Body's Tip.

    ``Body.Tip`` is what the Body *shows*, so claiming it for a feature that is
    not the end of the chain silently hides everything after it. A dress-up on a
    pad must therefore NOT take the tip away from the pattern that follows it —
    only a successor of the current tip (``feat_depends_on_tip``, i.e. the tip is
    among the feature's OutList) or the first feature of a fresh body may.
    """
    if not advances_tip(type_id) or tip_is_feat:
        return False
    return feat_depends_on_tip if body_has_tip else True


def _dress(kind: str) -> dict[str, str]:
    try:
        return _DRESS[str(kind).lower()]
    except KeyError:
        raise ValueError(
            f"unknown dress feature {kind!r}; expected one of {sorted(_DRESS)}"
        ) from None


def dress_type(kind: str, base_in_body: bool) -> str:
    """The object type to build for a fillet/chamfer.

    A ``Part::Fillet`` is a document-root object: it is not in the Body's Group,
    does not follow the Body's Placement, and assigning it to ``Body.Tip`` is
    accepted silently and leaves the Body ``['Touched', 'Invalid']``. When the
    base feature belongs to a Body, build the PartDesign dress-up instead.
    """
    spec = _dress(kind)
    return spec["pd"] if base_in_body else spec["part"]


def dress_size_property(kind: str) -> str:
    """The scalar size property of the PartDesign dress-up (1.1 has no tuples)."""
    return _dress(kind)["prop"]


def dress_spec_key(kind: str) -> str:
    """The cad() spec key carrying the size — 'radius'/'size', NOT the op name.

    The op is called "fillet" but its spec key is "radius"; conflating the two
    made every fillet op fail with "fillet requires: fillet".
    """
    return _dress(kind)["spec"]


def dress_base_is_allowed(*, base_in_body: bool, base_is_tip: bool) -> bool:
    """Whether a dress-up on this base can be built without wrecking the body.

    A Part-level base goes down the Part::Fillet path and touches no body.
    Inside a Body, the dress-up must sit at the END of the chain: FreeCAD 1.1
    moves ``Body.Tip`` onto a newly created PartDesign feature, so dressing a
    mid-chain feature makes the body show the dress-up and silently DROP every
    later feature — measured live: filleting the pad under a flange put the body
    at 15999 mm^3 with the six bolt holes gone. There is no safe API for the
    mid-chain insert either (``Body.insertObject`` duplicated the Group entry and
    left the tip wrong), so the caller must refuse.
    """
    return (not base_in_body) or base_is_tip
