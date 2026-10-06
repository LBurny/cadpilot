"""Parametric feature creation for the RPC ``create_feature`` handler.

Every feature is a live FreeCAD object (Part::Fillet, Part::Loft, ...)
linked to its source objects, so edits to sources recompute downstream.
Builders raise on any error; the caller (_run_op_with_screenshot) aborts
the transaction, so a failed feature leaves no residue.
"""

import contextlib
from typing import Any

import FreeCAD
import Part

from rpc_server import sketcher_ops, tip_policy
from rpc_server.geometry_query import body_owner
from rpc_server.property_mapper import format_color, parse_color

# Selector resolution of the most recent face-based build; read once by
# describe_feature() (mirrors sketcher_ops' sketch-info mechanism).
_LAST_FEATURE_INFO: dict | None = None

FEATURE_TYPES = (
    "boolean",
    "fillet",
    "chamfer",
    "loft",
    "sweep",
    "mirror",
    "pattern",
    "move",
    "color",
    "variables",
    "sketch",
    "pad",
    "pocket",
    "revolution",
    "groove",
    "thickness",
    "draft",
    "datum_plane",
    "hull",
)

_AXIS = {"X": (1, 0, 0), "Y": (0, 1, 0), "Z": (0, 0, 1)}
_MIRROR_PLANE = {"XY": (0, 0, 1), "XZ": (0, 1, 0), "YZ": (1, 0, 0)}


def _require(spec, *keys):
    missing = [k for k in keys if spec.get(k) is None]
    if missing:
        raise ValueError(f"{spec.get('type')} requires: {', '.join(missing)}")


def _get_obj(doc, name, role="object"):
    obj = doc.getObject(name)
    if obj is None:
        raise ValueError(f"{role} '{name}' not found in document '{doc.Name}'.")
    return obj


def _is_direction_token(item: str) -> bool:
    return item.startswith(("+", "-")) or item.lower() in sketcher_ops._FACE_WORDS


def _resolve_elements(obj, selector, kind):
    """Selector -> sub-element names. kind: 'Edge' or 'Face'.

    Faces also accept the direction tokens a sketch's plane.face takes
    ('+Z', 'top', 'MaxX', …): face names are re-derived after every feature, so
    an LLM that knows the direction but not the generated name had to run a
    get_topology round-trip first (live workflow feedback). 'all' and explicit
    index/name lists work exactly as before.
    """
    elements = getattr(obj.Shape, "Edges" if kind == "Edge" else "Faces")
    all_names = [f"{kind}{i + 1}" for i in range(len(elements))]
    if selector == "all":
        return all_names
    if isinstance(selector, str):
        if kind == "Face" and _is_direction_token(selector):
            return [sketcher_ops._resolve_semantic_face(obj, selector)]
        raise ValueError(f"selector must be 'all' or a non-empty list, got {selector!r}")
    if not isinstance(selector, list) or not selector:
        raise ValueError(f"selector must be 'all' or a non-empty list, got {selector!r}")
    names = []
    for item in selector:
        if isinstance(item, int):
            if not 0 <= item < len(all_names):
                raise ValueError(f"{kind} index {item} out of range (0-{len(all_names) - 1}).")
            names.append(all_names[item])
        elif isinstance(item, str):
            if item in all_names:
                names.append(item)
                continue
            if kind == "Face" and _is_direction_token(item):
                names.append(sketcher_ops._resolve_semantic_face(obj, item))
                continue
            raise ValueError(f"'{item}' not a valid sub-element (e.g. {all_names[0]}).")
        else:
            raise ValueError(f"selector items must be int or str, got {item!r}")
    return names


def _record_resolved_faces(base, names) -> None:
    """Echo the faces a selector resolved to, so a wrong pick is visible.

    A direction token picks the FARTHEST face facing that way; on a stepped
    part that can be a different face than the caller pictured, and without an
    echo the result looked exactly like success (live: a circle meant for the
    seat face landed on a wall top 92 mm above it). Faces facing the same way
    are listed as ``same_direction`` — on a phone stand the "+Z" pick was
    right, it was just one of three upward faces, and the alternative was
    only discoverable by trial and error.
    """
    global _LAST_FEATURE_INFO
    info = []
    for name in names:
        entry = {"face": name}
        with contextlib.suppress(Exception):
            face = base.Shape.getElement(name)
            if face is not None:
                entry["center"] = [round(float(v), 4) for v in face.CenterOfMass]
                if face.Surface.TypeId == "Part::GeomPlane":
                    normal = face.normalAt(0, 0)
                    entry["normal"] = [round(float(v), 4) for v in normal]
                    same = []
                    for i, f in enumerate(base.Shape.Faces):
                        if f.Surface.TypeId != "Part::GeomPlane" or f.isSame(face):
                            continue
                        if f.normalAt(0, 0).dot(normal) > 0.999:
                            same.append(
                                {
                                    "face": f"Face{i + 1}",
                                    "center": [round(float(v), 4) for v in f.CenterOfMass],
                                    "area": round(float(f.Area), 4),
                                }
                            )
                    if same:
                        entry["same_direction"] = sorted(same, key=lambda e: -e["area"])[:6]
        info.append(entry)
    _LAST_FEATURE_INFO = {"base": base.Name, "faces": info}


def pop_last_feature_info() -> dict | None:
    """The last selector resolution, read once by describe_feature()."""
    global _LAST_FEATURE_INFO
    info = _LAST_FEATURE_INFO
    _LAST_FEATURE_INFO = None
    return info


def _axis_vec(value, default="Z"):
    v = _AXIS.get(str(value or default).upper())
    if v is None:
        raise ValueError(f"axis must be 'X'/'Y'/'Z', got {value!r}")
    return FreeCAD.Vector(*v)


def _inherit_appearance(feat, base):
    """Copy color/transparency from the base solid to a feature result.

    Part booleans create a fresh ViewObject in the default gray, silently
    dropping whatever color the user set on the base. Only the two
    appearance properties that matter for solids are copied; every access
    is guarded because ViewObject is absent in console mode and the
    assignment can fail on exotic view providers.
    """
    src = getattr(base, "ViewObject", None)
    dst = getattr(feat, "ViewObject", None)
    if src is None or dst is None:
        return
    for prop in ("ShapeColor", "Transparency"):
        with contextlib.suppress(Exception):
            setattr(dst, prop, getattr(src, prop))


def _build_boolean(doc, spec):
    _require(spec, "op", "base", "tool")
    op = spec["op"]
    if op not in ("fuse", "cut", "common"):
        raise ValueError(f"boolean op must be fuse/cut/common, got {op!r}")
    base_obj = _get_obj(doc, spec["base"], "base")
    tool_val = spec["tool"]
    if isinstance(tool_val, list):
        if not tool_val:
            raise ValueError("boolean tool list must not be empty.")
        tools = [_get_obj(doc, name, "tool") for name in tool_val]
    else:
        tools = [_get_obj(doc, tool_val, "tool")]
    name = spec.get("name") or op.capitalize()
    if len(tools) == 1:
        feat = doc.addObject(
            {"fuse": "Part::Fuse", "cut": "Part::Cut", "common": "Part::Common"}[op], name
        )
        feat.Base, feat.Tool = base_obj, tools[0]
    elif op == "fuse":
        # Part::MultiFuse, NOT a compound Tool: a compound of OVERLAPPING tools
        # does not merge them, and the result double-counts the overlap —
        # measured live on a box + arm + knuckle, a compound Tool gave 17461.9
        # mm^3 against the true 15765.5, with the knuckle only partly present.
        feat = doc.addObject("Part::MultiFuse", name)
        feat.Shapes = [base_obj, *tools]
    elif op == "common":
        # "common" with several tools means intersecting ALL of them;
        # Part::MultiCommon is FreeCAD's own form of that. (A compound Tool
        # computes base ∩ (T1 ∪ T2) instead — measured 800 vs 0 mm^3 for the
        # same inputs, a silent semantic flip.)
        feat = doc.addObject("Part::MultiCommon", name)
        feat.Shapes = [base_obj, *tools]
    else:  # cut: base - (T1 ∪ T2), the chained-cut result (verified equal)
        union = doc.addObject("Part::MultiFuse", f"{name}_tools")
        union.Shapes = tools
        doc.recompute()
        view = getattr(union, "ViewObject", None)
        if view is not None:
            view.Visibility = False
        feat = doc.addObject("Part::Cut", name)
        feat.Base, feat.Tool = base_obj, union
    _inherit_appearance(feat, base_obj)
    return feat


def _require_end_of_chain(body, base, op):
    """Refuse a dress-up whose base is not the end of its Body's chain.

    FreeCAD moves ``Body.Tip`` onto a newly created PartDesign feature, so
    dressing a mid-chain feature makes the body SHOW the dress-up and silently
    drop everything after it — measured live: a fillet on the pad under a flange
    left the body at 15999 mm^3 with the six bolt holes gone. PartDesign offers no
    safe mid-chain insert through the API (``Body.insertObject`` duplicated the
    Group entry and still left the tip wrong), so the op refuses. The caller runs
    it in a transaction, so refusing leaves the document untouched.
    """
    tip = getattr(body, "Tip", None)
    if tip_policy.dress_base_is_allowed(base_in_body=True, base_is_tip=tip is base):
        return
    tip_name = tip.Name if tip is not None else "none"
    raise ValueError(
        f"{op} base '{base.Name}' is inside Body '{body.Name}' but is not its Tip "
        f"({tip_name}). A dress-up is built at the END of the chain, and FreeCAD would "
        "move the Body's Tip onto it, silently dropping every later feature (pockets and "
        "patterns included). Dress the Tip instead, or reorder the features in FreeCAD's GUI."
    )


def _build_fillet_chamfer(doc, spec, kind):
    """Build a fillet/chamfer where it belongs: inside the Body when the base
    lives in one, or IS one, at the document root otherwise.

    A ``Part::Fillet`` is a document-root object — it is not in the Body's
    Group, it does not follow the Body's Placement, and ``Body.Tip = <it>`` is
    accepted silently while leaving the Body ``['Touched', 'Invalid']``. A base
    that is a bare Part-level object still wants the Part::Fillet path.
    """
    # Function-top on purpose: the bare-base branch below also writes
    # _LAST_FEATURE_INFO, and when the declaration lived inside the named_body
    # branch that write only reached the global through Python's
    # whole-function scoping — removing that branch would have silenced the
    # hidden_base report (the same trap _build_color was caught by).
    global _LAST_FEATURE_INFO
    size_key = tip_policy.dress_spec_key(kind)
    _require(spec, "base", "edges", size_key)
    base = _get_obj(doc, spec["base"], "base")
    body = _parent_body(base)
    target = base
    named_body = False
    if body is None and getattr(base, "TypeId", "") == "PartDesign::Body":
        # Naming the BODY is how a caller naturally asks for "round the rim of
        # this part", but a Body has no parent Body, so this used to fall
        # through to the root-level ``Part::Fillet``: outside the Body's Group,
        # Body.Tip still on the last feature, and the Body's displayed shape
        # never changed while the reply said "created successfully" (live: a 2mm
        # fillet on a bore rim left the Body at 17591.5 mm^3, the filleted
        # 17545.9 sitting beside it as a second, invisible solid). The GUI does
        # the PartDesign thing here: dress the Body's Tip, inside the Body.
        body = base
        target = getattr(base, "Tip", None)
        named_body = True
        if target is None:
            raise ValueError(
                f"Body '{base.Name}' has no Tip, so there is nothing to {kind}: it holds no "
                "feature yet. Build the feature first (pad/pocket/…), then dress its edges."
            )
    names = _resolve_elements(target, spec["edges"], "Edge")
    size = float(spec[size_key])
    label = spec.get("name") or f"{kind.capitalize()}"
    if body is not None:
        _require_end_of_chain(body, target, kind)
        # FreeCAD 1.1's PartDesign dress-up holds ONE scalar size for all edges
        # (the per-edge tuple form is Part-level only).
        feat = body.newObject(tip_policy.dress_type(kind, True), label)

        def _probe(subset, feat=feat, target=target, doc=doc):
            feat.Base = (target, list(subset))
            doc.recompute()
            return _dress_shape_usable(feat)

        feat.Base = (target, names)
        setattr(feat, tip_policy.dress_size_property(kind), size)
        _remember_dress_context(kind, size, names, _probe)
        if named_body:
            # The caller named the BODY, so the reply must say which of its
            # features actually got dressed: "which feature is the Tip" is the
            # one thing a Body-named dress-up resolves for them.
            _LAST_FEATURE_INFO = {"dressed": target.Name, "body": body.Name}
        return feat
    feat = doc.addObject(tip_policy.dress_type(kind, False), label)
    if hasattr(feat, "EdgeLinks"):
        # FreeCAD >= 1.1 rework: Base is a plain link and per-edge sizes live
        # in Edges as (1-based edge index, size_start, size_end) tuples.
        feat.Base = base
        feat.Edges = [(int(n[4:]), size, size) for n in names]

        def _probe(subset, feat=feat, base=base, doc=doc, size=size):
            feat.Base = base
            feat.Edges = [(int(n[4:]), size, size) for n in subset]
            doc.recompute()
            return _dress_shape_usable(feat)

    else:
        feat.Base = (base, names)
        setattr(feat, tip_policy.dress_size_property(kind), size)

        def _probe(subset, feat=feat, base=base, doc=doc):
            feat.Base = (base, list(subset))
            doc.recompute()
            return _dress_shape_usable(feat)

    _remember_dress_context(kind, size, names, _probe)
    # The base is redundant once its own rim is dressed (same solid, sharper
    # edges) and FreeCAD draws BOTH, so the caller was left with two
    # overlapping solids and no hint which one to keep (live: a fillet on a
    # bare Part::Box left both visible). The color is inherited first, so
    # hiding the base cannot expose a default-gray feature where a colored
    # part was. The base's data is untouched — only its display.
    _inherit_appearance(feat, base)
    view = getattr(base, "ViewObject", None)
    if view is not None:
        with contextlib.suppress(Exception):
            view.Visibility = False
        _LAST_FEATURE_INFO = {"hidden_base": base.Name}
    return feat


def _build_loft(doc, spec):
    _require(spec, "profiles")
    profiles = [_get_obj(doc, n, "profile") for n in spec["profiles"]]
    if len(profiles) < 2:
        raise ValueError("loft requires at least 2 profiles.")
    # Loft has no base object (CAD_NO_BASE_OPERATIONS), so cad()'s obj_name
    # arrives as spec["base"] — that is the documented name of the NEW loft.
    # Reading only spec["name"] silently named every loft "Loft".
    feat = doc.addObject("Part::Loft", spec.get("name") or spec.get("base") or "Loft")
    feat.Sections = profiles
    feat.Solid = bool(spec.get("solid", True))
    feat.Ruled = bool(spec.get("ruled", False))
    return feat


def _build_sweep(doc, spec):
    _require(spec, "base", "path")
    feat = doc.addObject("Part::Sweep", spec.get("name") or "Sweep")
    feat.Sections = [_get_obj(doc, spec["base"], "profile")]
    feat.Spine = _get_obj(doc, spec["path"], "path")
    feat.Solid = bool(spec.get("solid", True))
    return feat


def _build_mirror(doc, spec):
    _require(spec, "base")
    base = _get_obj(doc, spec["base"], "base")
    # FreeCAD >= 1.0 renamed Part::Mirror to Part::Mirroring.
    # Try the new name first; fall back only if the type does not exist.
    mirror_type = "Part::Mirroring"
    try:
        feat = doc.addObject(mirror_type, spec.get("name") or "Mirror")
    except Exception:
        # Only fall back if the type itself is unknown; re-raise other errors.
        try:
            mirror_type = "Part::Mirror"
            feat = doc.addObject(mirror_type, spec.get("name") or "Mirror")
        except Exception:
            raise ValueError(
                "Neither Part::Mirroring nor Part::Mirror is available in this FreeCAD version."
            ) from None
    feat.Source = base
    face_sel = spec.get("face")
    if face_sel is not None:
        items = face_sel if isinstance(face_sel, list) else [face_sel]
        names = _resolve_elements(base, items, "Face")
        face = base.Shape.Faces[int(names[0][4:]) - 1]
        umin, umax, vmin, vmax = face.ParameterRange
        feat.Normal = face.normalAt((umin + umax) / 2, (vmin + vmax) / 2)
        feat.Base = face.CenterOfMass
    else:
        normal = _MIRROR_PLANE.get(str(spec.get("plane", "XY")).upper())
        if normal is None:
            raise ValueError(f"mirror plane must be XY/XZ/YZ, got {spec.get('plane')!r}")
        feat.Normal = FreeCAD.Vector(*normal)
        feat.Base = FreeCAD.Vector(0, 0, 0)
    return feat


def _parent_body(obj):
    """The PartDesign::Body ``obj`` belongs to, or None (a Part-level object)."""
    for o in getattr(obj, "InList", []):
        if o.TypeId == "PartDesign::Body":
            return o
    return None


def _body_axis_datum(body, axis: str):
    """The body origin's axis datum (App::Line) for 'X'/'Y'/'Z', or None."""
    origin = getattr(body, "Origin", None)
    role = f"{axis.upper()}_Axis"
    return next(
        (f for f in getattr(origin, "OriginFeatures", []) if getattr(f, "Role", "") == role),
        None,
    )


def _centered_axis_line(body, axis: str, center):
    """A datum line through ``center`` along the body's X/Y/Z axis.

    A PartDesign::PolarPattern rotates about the AXIS LINE it references, and
    the body's origin datum line runs through the body origin — so a bolt
    circle about a face centre (the common intent) would silently clip every
    occurrence that falls outside the material. FreeCAD's own datum line is the
    reference the GUI uses for this; a free Placement is what positions it
    (MapMode "Translate" over the origin line does NOT move it — live-verified).
    """
    vec = _vec3(center, "center")
    line = body.newObject("PartDesign::Line", "CadPilotPolarAxis")
    line.MapMode = "Deactivated"
    axis_rot = {"Z": FreeCAD.Rotation(), "Y": FreeCAD.Rotation(FreeCAD.Vector(1, 0, 0), -90.0)}
    rot = axis_rot.get(axis) or FreeCAD.Rotation(FreeCAD.Vector(0, 1, 0), 90.0)
    line.Placement = FreeCAD.Placement(vec, rot)
    return line


def _build_pd_pattern(doc, body, base, spec, ptype: str, count: int):
    """Pattern a PartDesign FEATURE inside its Body.

    Draft's array replicates the base object's whole Shape — for a PartDesign
    feature that Shape is the entire body, so arraying a hole produced N
    overlapping copies of the whole part instead of N holes in one part. A
    PartDesign::(Polar|Linear)Pattern with ``Originals=[feature]`` repeats the
    feature's own contribution, which is what "pattern this hole" means.
    """
    axis = str(spec.get("axis", "Z")).upper()
    if axis not in _REV_AXIS:
        raise ValueError(f"axis must be 'X'/'Y'/'Z' for a PartDesign pattern, got {axis!r}")
    datum = _body_axis_datum(body, axis)
    if ptype == "polar":
        feat = body.newObject("PartDesign::PolarPattern", spec.get("name") or "PolarPattern")
        _set_or_bind(feat, "Angle", spec.get("angle", 360.0))
        # ``center`` was silently ignored here (only the Draft path read it), so
        # a caller's bolt circle about a face centre rotated about the body
        # origin instead and lost the occurrences that fell outside the plate —
        # reported as plain success.
        center_line = (
            _centered_axis_line(body, axis, spec["center"]) if spec.get("center") else None
        )
        if center_line is not None:
            feat.Axis = (center_line, [""])
        elif datum is not None:
            feat.Axis = (datum, [""])
    elif ptype == "linear":
        _require(spec, "spacing")
        feat = body.newObject("PartDesign::LinearPattern", spec.get("name") or "LinearPattern")
        # Length spans the pattern (count-1 gaps), matching the Draft semantics.
        _set_or_bind(feat, "Length", float(spec["spacing"]) * (count - 1))
        if datum is not None:
            feat.Direction = (datum, [""])
    else:
        raise ValueError(f"pattern_type must be linear/polar, got {ptype!r}")
    # AFTER the transform props: assigning Originals re-derives the shape.
    feat.Originals = [base]
    feat.Occurrences = count
    feat.Reversed = bool(spec.get("reversed", False))
    doc.recompute()
    # FreeCAD 1.1 cannot be driven reliably into doing this through the Python
    # API: with Originals/Axis/Angle/Occurrences set, the occurrences can come
    # out coincident, so the pattern computes and repeats NOTHING. Returning a
    # part with one hole where six were asked for is the worst outcome, so
    # refuse loudly instead (verified live — see the docs for the workaround).
    try:
        unchanged = abs(float(feat.Shape.Volume) - float(base.Shape.Volume)) < 1e-6
    except Exception:
        unchanged = False
    if unchanged:
        raise RuntimeError(
            f"pattern of PartDesign feature '{base.Name}' had no effect (volume unchanged at "
            f"{round(float(base.Shape.Volume), 1)} mm^3). A PartDesign pattern cannot be driven "
            "reliably through this API; pattern the PROFILE instead (one pocket per instance) or "
            "pattern a Part-level solid with boolean ops. Not creating a misleading result."
        )
    return feat


@contextlib.contextmanager
def _active_document(doc):
    """Make ``doc`` FreeCAD's ACTIVE document for the duration of the block.

    Draft's array helpers create their Array in ``FreeCAD.ActiveDocument``, so
    patterning a document that is merely OPEN raised "PropertyLink does not
    support external object" — multi-document sessions (the norm, and what a
    parallel test run produces) failed on a call that works when the document
    happens to be active. The previous active document is restored afterwards
    so driving another document does not yank the user's view away.
    """
    previous = None
    with contextlib.suppress(Exception):
        active = FreeCAD.ActiveDocument
        previous = active.Name if active is not None else None
    if previous != doc.Name:
        FreeCAD.setActiveDocument(doc.Name)
    try:
        yield
    finally:
        if previous and previous != doc.Name:
            with contextlib.suppress(Exception):
                FreeCAD.setActiveDocument(previous)


def _pattern_count(doc, raw) -> int:
    """Resolve the pattern count, which may be an expression binding.

    ``count="=Vars.n_holes"`` used to die inside ``int()`` ("invalid literal
    for int() with base 10"), so a Spreadsheet could not drive an array even
    though every other op accepts "=expr". A throwaway object borrows FreeCAD's
    expression engine to evaluate it up front; it is created and removed inside
    the op's own transaction, so it leaves no trace in the document.
    """
    if isinstance(raw, str) and raw.startswith("="):
        probe = doc.addObject("App::FeaturePython", "_CADPilotExprProbe")
        invalid = False
        value = 0.0
        try:
            probe.addProperty("App::PropertyFloat", "Value")
            probe.setExpression("Value", raw[1:])
            doc.recompute()
            # An alias that does not exist does NOT raise: the property simply
            # stays 0 and goes Invalid, which then surfaced as a baffling
            # "pattern count must be >= 2" instead of naming the bad expression.
            invalid = "Invalid" in [str(s) for s in getattr(probe, "State", [])]
            value = float(probe.Value)
        except Exception as e:
            raise ValueError(
                f"pattern count expression {raw!r} could not be evaluated ({e})."
            ) from None
        finally:
            with contextlib.suppress(Exception):
                doc.removeObject(probe.Name)
        if invalid:
            raise ValueError(
                f"pattern count expression {raw!r} did not resolve to a number — check the "
                "spreadsheet alias/object name (a misspelled alias evaluates to 0, not an error)."
            )
        return round(value)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"pattern count must be an integer >= 2 or an expression like '=Vars.n_holes', "
            f"got {raw!r}."
        ) from None


def _bind_count_expression(feat, raw, ptype: str) -> None:
    """Keep the pattern count tied to its spreadsheet expression.

    ``_pattern_count`` resolves "=Vars.n_holes" once; without the binding the
    pattern froze at the value the spreadsheet had on creation day (live: a
    flange stayed at 6 holes after n_holes became 8, while pad and pocket
    followed their aliases) — valid geometry, silently the wrong model.
    PartDesign patterns take Occurrences, Draft arrays NumberPolar/NumberX.
    """
    if not (isinstance(raw, str) and raw.startswith("=")):
        return
    props = ("NumberPolar", "NumberX") if ptype == "polar" else ("NumberX",)
    for prop in (*props, "Occurrences"):
        if prop not in list(getattr(feat, "PropertiesList", []) or []):
            continue
        with contextlib.suppress(Exception):
            feat.setExpression(prop, raw[1:])
        return


def _build_pattern(doc, spec):
    _require(spec, "base", "count")
    base = _get_obj(doc, spec["base"], "base")
    count = _pattern_count(doc, spec["count"])
    if count < 2:
        raise ValueError("pattern count must be >= 2.")
    ptype = str(spec.get("pattern_type", "linear"))
    if ptype not in ("linear", "polar"):
        raise ValueError(f"pattern_type must be linear/polar, got {ptype!r}")
    body = _parent_body(base)
    if body is not None:
        feat = _build_pd_pattern(doc, body, base, spec, ptype, count)
        _bind_count_expression(feat, spec.get("count"), ptype)
        return feat

    import Draft

    make_array = getattr(Draft, "make_array", None) or Draft.makeArray
    with _active_document(doc):
        if ptype == "linear":
            _require(spec, "spacing")
            direction = _axis_vec(spec.get("axis"), default="X")
            feat = make_array(
                base, direction * float(spec["spacing"]), FreeCAD.Vector(0, 0, 0), count, 1
            )
        else:
            center = _vec3(spec.get("center", [0, 0, 0]), "center")
            angle = float(spec.get("angle", 360.0))
            feat = make_array(base, center, angle, count)
            axis = str(spec.get("axis", "Z")).upper()
            if axis != "Z" and hasattr(feat, "Axis"):
                feat.Axis = _axis_vec(axis)
    if spec.get("name"):
        feat.Label = spec["name"]
    _bind_count_expression(feat, spec.get("count"), ptype)
    return feat


def _set_or_bind(obj, prop, value):
    """Assign a property, or bind it to the ExpressionEngine when the value
    is a string starting with '=' (same semantics as property_mapper)."""
    if isinstance(value, str) and value.startswith("="):
        obj.setExpression(prop, value[1:])
    else:
        setattr(obj, prop, value)


def _build_variables(doc, spec):
    """Create or update a Spreadsheet parameter table (idempotent).

    spec: {"type": "variables", "base": name|None,
           "cells": {"A1": ["alias", value], ...}}
    Values: number -> literal; string starting with '=' -> formula;
    other strings -> quoted text cell.
    """
    cells = spec.get("cells")
    if not isinstance(cells, dict) or not cells:
        raise ValueError("variables requires a non-empty 'cells' dict.")
    name = spec.get("base") or spec.get("name") or "Spreadsheet"
    ss = doc.getObject(name)
    if ss is None:
        ss = doc.addObject("Spreadsheet::Sheet", name)
    # The same setter ``edit_object`` uses for cells: shared so both paths
    # validate identically ("Invalid alias" alone names nothing).
    from .property_mapper import set_spreadsheet_cells

    set_spreadsheet_cells(ss, cells)
    doc.recompute()
    return ss


def _vec3(v, name):
    """Vector as [x, y, z] or {"x":.., "y":.., "z":..} -> FreeCAD.Vector.

    The rest of the API takes plain coordinate lists; dict-only here used to
    crash with AttributeError on the natural list form.
    """
    if isinstance(v, dict):
        return FreeCAD.Vector(float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0)))
    if isinstance(v, (list, tuple)) and len(v) == 3:
        return FreeCAD.Vector(float(v[0]), float(v[1]), float(v[2]))
    raise ValueError(f"{name} must be [x, y, z] or {{'x':.., 'y':.., 'z':..}}, got {v!r}")


def _build_move(doc, spec):
    """Apply a relative translation and/or rotation to an existing object.

    This is NOT a parametric feature — it directly modifies the object's Placement.
    Supported spec keys (vectors accept [x,y,z] or {"x":..,"y":..,"z":..}):
      translate: [dx, dy, dz] or {"x": dx, "y": dy, "z": dz}  — relative translation
      rotate: {"axis": [ax,ay,az], "angle": degrees}  — relative rotation
      placement: {"Base": [x,y,z], "Rotation": {"Axis": [...], "Angle": deg}}  — absolute override
    If both translate/rotate and placement are given, placement wins (absolute).
    """
    _require(spec, "base")
    obj = _get_obj(doc, spec["base"], "base")
    # PartDesign features do not own their frame: FreeCAD rewrites their
    # Placement on the next recompute (live-verified — a moved Pad returned to
    # (0,0,0) on the next cad() call, and the boolean that followed fused the
    # UN-MOVED shape). Moving the owning Body is the only persistent version of
    # the same intent; describe_feature reports the redirect.
    owner = body_owner(obj)
    if owner is not None:
        obj = owner
    current = obj.Placement

    # Absolute placement override
    if "placement" in spec:
        p = spec["placement"]
        base = p.get("Base", p.get("Position", {"x": 0, "y": 0, "z": 0}))
        rot_data = p.get("Rotation", {"Axis": {"x": 0, "y": 0, "z": 1}, "Angle": 0})
        new_base = _vec3(base, "placement.Base")
        axis = rot_data.get("Axis", {"x": 0, "y": 0, "z": 1})
        new_rot = FreeCAD.Rotation(
            _vec3(axis, "placement.Rotation.Axis"),
            float(rot_data.get("Angle", 0)),
        )
        return _assign_placement(doc, obj, new_base, new_rot)

    # Relative translation / rotation — at least one is required.
    translate = spec.get("translate", {})
    rotate = spec.get("rotate", {})
    if not translate and not rotate:
        raise ValueError("move requires at least one of: translate, rotate, placement.")
    delta = _vec3(translate, "translate") if translate else FreeCAD.Vector(0, 0, 0)

    # Relative rotation
    delta_rot = FreeCAD.Rotation()
    if rotate:
        if not isinstance(rotate, dict):
            raise ValueError(f"rotate must be a dict {{'axis':…, 'angle':…}}, got {rotate!r}")
        r_axis = _vec3(rotate.get("axis", {"x": 0, "y": 0, "z": 1}), "rotate.axis")
        r_angle = float(rotate.get("angle", 0))  # degrees
        delta_rot = FreeCAD.Rotation(r_axis, r_angle)

    # Compose the new placement. Translation is always in the GLOBAL frame;
    # rotation is applied around the object's current placement base.
    if translate and rotate:
        # Translate first (global), then rotate around the new position
        new_base = current.Base + delta
        new_rot = delta_rot.multiply(current.Rotation)
    elif translate:
        new_base = current.Base + delta
        new_rot = current.Rotation
    elif rotate:
        new_base = current.Base
        new_rot = delta_rot.multiply(current.Rotation)
    return _assign_placement(doc, obj, new_base, new_rot)


def _assign_placement(doc, obj, new_base, new_rot):
    """Write the Placement, then verify it survives a recompute.

    A move that FreeCAD silently undoes (an attached sketch or datum plane
    recomputes its Placement away) must not be reported as success — the
    readback turns it into an honest failure that rolls the whole op back.
    """
    obj.Placement = FreeCAD.Placement(new_base, new_rot)
    doc.recompute()
    back = obj.Placement
    drift = (back.Base - new_base).Length
    with contextlib.suppress(Exception):
        drift += abs(back.Rotation.multiply(new_rot.inverted()).Angle) * 100.0  # deg -> ~mm
    if drift > 1e-4:
        raise RuntimeError(
            f"move did not persist on '{obj.Name}': it is back at "
            f"[{round(back.Base.x, 4)}, {round(back.Base.y, 4)}, {round(back.Base.z, 4)}] "
            "after a recompute. Attached sketches/datum planes get their Placement from "
            "their attachment (move the supporting face or change the offset instead), and "
            "a PartDesign feature is positioned by its Body."
        )
    return obj


# --- appearance ----------------------------------------------------------------
#
# A color op writes the ViewObject's appearance properties and nothing else: it
# adds no feature, needs no recompute and has no geometry to validate. It is a
# feature op purely so it inherits the whole machinery a step needs — its own
# transaction (live-verified on 1.1.4: a ViewObject write inside a transaction
# DOES produce an undo entry, so the color is a rollback-able, replayable step),
# the journal record, the session step and the label grammar.

#: obj_properties keys the color op understands. A key outside this set is
#: ignored by the other builders, but here "nothing recognized" would be a
#: silent no-op, so an unrecognized-only call is refused.
_APPEARANCE_KEYS = (
    "color",
    "transparency",
    "line_color",
    "line_width",
    "point_size",
    "display_mode",
    "draw_style",
    "visible",
)

#: obj_name values that mean "every object in the document".
_COLOR_ALL = ("*", "all", "every")

#: How many per-object entries the reply carries. A wildcard sweep on a real
#: model paints every object, and the answer is the COUNT plus which ones were
#: redirected or skipped: dumping one entry per object into the model's context
#: is the same context cost the screenshot pipeline was rebuilt to avoid.
_COLOR_REPORT_MAX = 20


def _parse_transparency(value, key="transparency"):
    """FreeCAD's ``Transparency`` is a PERCENTAGE 0-100 (an int)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number 0-100 (percent), got {value!r}.")
    num = float(value)
    if 0.0 < num < 1.0:
        raise ValueError(
            f"{key}={value!r} looks like a fraction; FreeCAD reads this as a percentage. "
            f"Pass {round(num * 100)} to mean {round(num * 100)}%."
        )
    if not 0.0 <= num <= 100.0:
        raise ValueError(f"{key} must be 0-100 (percent), got {value!r}.")
    return round(num)


def _parse_enum(view, prop, value, key):
    """Validate an enumerated ViewObject property against FreeCAD's own list.

    The list is read off the live view provider instead of being hardcoded: a
    Body, a Part feature and a TechDraw view do not share one. A provider that
    offers no list at all (an exotic custom ViewProvider) cannot be validated,
    so the value passes through and the write itself decides.
    """
    choices = []
    with contextlib.suppress(Exception):
        choices = [str(c) for c in view.getEnumerationsOfProperty(prop) or []]
    text = str(value).strip()
    if not choices:
        return text
    for choice in choices:
        if choice.lower() == text.lower():
            return choice
    raise ValueError(f"{key} must be one of {', '.join(choices)}, got {value!r}.")


def _color_targets(doc, spec):
    """The (object, redirect note, named-explicitly) triples a color op applies to.

    ``obj_name`` names one object, or uses ``*``/``all`` for every object in
    the document; an optional ``objects`` list names several at once. A
    PartDesign feature is redirected to its owning Body: the feature's own
    ViewObject is hidden, so coloring it changes nothing a user or a render ever
    sees (the same redirect the move op needs, for the same reason — a feature
    owns neither its display nor its frame).

    The third element says whether the caller NAMED this object. A wildcard sweep
    meets objects that cannot be painted at all (a Spreadsheet's view provider
    has no ShapeColor, and `variables` puts one in every parametric document), and
    failing the whole sweep over one of them would make `obj_name="*"` useless on
    exactly the models worth coloring. An explicitly named target has no such
    excuse: it must fail.
    """
    raw: list[tuple[str, bool]] = []
    if spec.get("base"):
        raw.append((spec["base"], True))
    extra = spec.get("objects")
    if isinstance(extra, str):
        extra = [extra]
    if isinstance(extra, (list, tuple)):
        raw.extend((item, True) for item in extra)

    names: list[tuple[str, bool]] = []
    for item, explicit in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"color targets must be object names, got {item!r}.")
        name = item.strip()
        if name.lower() in _COLOR_ALL:
            names.extend((o.Name, False) for o in doc.Objects)
        else:
            names.append((name, explicit))
    if not names:
        raise ValueError(
            "color requires obj_name (an object name, or '*' for every object in the document)."
        )

    targets: list[tuple[object, str, bool]] = []
    seen: set[str] = set()
    for name, explicit in names:
        obj = _get_obj(doc, name, "target")
        note = ""
        owner = body_owner(obj)
        if owner is not None and owner is not obj:
            note = (
                f"'{obj.Name}' is a PartDesign feature; its own ViewObject is hidden, so the "
                f"appearance was applied to its Body '{owner.Name}'."
            )
            obj = owner
        if obj.Name in seen:
            continue
        seen.add(obj.Name)
        targets.append((obj, note, explicit))
    if not targets:
        raise ValueError("color matched no object in the document.")
    return targets


#: appearance spec key -> the ViewObject property it writes. Used to REFUSE a
#: target that cannot carry the property at all, instead of letting FreeCAD's
#: raw AttributeError out (which names the property but not the object).
_APPEARANCE_PROPS = {
    "color": "ShapeColor",
    "transparency": "Transparency",
    "line_color": "LineColor",
    "line_width": "LineWidth",
    "point_size": "PointSize",
    "display_mode": "DisplayMode",
    "draw_style": "DrawStyle",
    "visible": "Visibility",
}


def _fresh_material(src, diffuse=None, transparency=None):
    """A NEW ``Material`` copying ``src``'s channels, optionally overridden.

    A fresh object is the whole point: FreeCAD treats an identical assignment as
    a no-op (no ``onChanged``, so no display-node rebuild), which is why a
    same-value write does not repaint.
    """
    factory = getattr(FreeCAD, "Material", None)
    mat = factory() if factory is not None else src
    for prop in ("AmbientColor", "EmissiveColor", "SpecularColor", "Shininess"):
        with contextlib.suppress(Exception):
            setattr(mat, prop, getattr(src, prop))
    with contextlib.suppress(Exception):
        rgb = tuple(diffuse) if diffuse is not None else tuple(src.DiffuseColor)
        mat.DiffuseColor = rgb
    with contextlib.suppress(Exception):
        alpha = (
            transparency if transparency is not None else float(getattr(src, "Transparency", 0.0))
        )
        mat.Transparency = float(alpha)
    return mat


def _uniform_material(entries):
    """``entries[0]`` when every entry carries the same visible channels, else None.

    None means a genuine per-face design, which must not be flattened.
    """

    def key(mat):
        return (
            *(round(float(c), 4) for c in mat.DiffuseColor),
            round(float(getattr(mat, "Transparency", 0.0)), 4),
        )

    if not entries:
        return None
    first = key(entries[0])
    return entries[0] if all(key(m) == first for m in entries) else None


def _refresh_appearance(obj) -> bool:
    """Re-apply an object's appearance in place, forcing a display-node rebuild.

    Measured on 1.1.4: the 3D view draws the PartDesign BODY's node (hiding the
    Body hides the part; hiding the tip feature does not), and creating a new
    PartDesign feature makes it rebuild that node with FreeCAD's DEFAULT
    material. So the colour of a coloured Body visually VANISHES the moment a
    feature is added, while every stored value still says the new colour — the
    worst kind of silent state. Re-assigning the appearance with a FRESH material
    rebuilds the node and the colour comes back (live: (185,45,45) ->
    (110,116,120) after a fillet -> (184,45,44) after this call).

    A genuine per-face material list is left alone, and its render is NOT
    recoverable after a tip advance: four attempts all stayed default-gray
    (re-assigning the Body's list with fresh materials, copying the list onto the
    tip feature's ViewObject, ``obj.touch()`` + recompute, ``ViewObject.touch()``
    + ``updateGui()``). Flattening it would destroy a design the caller never
    asked to change, so the stored design is preserved and the view keeps
    FreeCAD's default until the caller re-colours (the color op does flatten, on
    purpose, because a whole-object colour is exactly what it promises).
    """
    view = getattr(obj, "ViewObject", None)
    if view is None:
        return False
    entries = list(getattr(view, "ShapeAppearance", ()) or ())
    src = _uniform_material(entries)
    if src is None:
        return False
    try:
        view.ShapeAppearance = (_fresh_material(src),)
        return True
    except Exception as exc:
        FreeCAD.Console.PrintWarning(
            f"CADPilot: could not refresh {obj.Name}'s appearance: {exc}\n"
        )
        return False


def _apply_appearance(obj, spec) -> dict:
    """Apply one object's appearance and read it back.

    Raises on anything that would make the write a silent no-op: a view provider
    that has no such property (a Spreadsheet, a TechDraw view — objects with no
    3D shape carry no appearance), or one that does not keep the color it was
    given. The caller decides what a failure means: an explicitly NAMED target
    fails the step, a wildcard sweep records it as skipped.
    """
    view = getattr(obj, "ViewObject", None)
    if view is None:
        raise RuntimeError(
            f"'{obj.Name}' has no ViewObject, so it cannot be colored (console mode / a "
            "document object without a view provider)."
        )
    missing = [
        f"{key} ({prop})"
        for key, prop in _APPEARANCE_PROPS.items()
        if key in spec and not hasattr(view, prop)
    ]
    if missing:
        raise RuntimeError(
            f"'{obj.Name}' ({type(view).__name__}) cannot take {', '.join(missing)}: it has no "
            "3D shape to paint (a Spreadsheet or a TechDraw view carries no appearance)."
        )
    applied = {}
    if "color" in spec:
        wanted = parse_color(spec["color"])
        view.ShapeColor = wanted  # writes through to the persisted ShapeAppearance
        applied["color"] = format_color(wanted)
    if "transparency" in spec:
        applied["transparency"] = _parse_transparency(spec["transparency"])
        view.Transparency = applied["transparency"]
    if "line_color" in spec:
        view.LineColor = parse_color(spec["line_color"])
        applied["line_color"] = format_color(spec["line_color"])
    if "line_width" in spec:
        width = float(spec["line_width"])
        if not width > 0:
            raise ValueError(f"line_width must be greater than 0, got {spec['line_width']!r}.")
        view.LineWidth = width
        applied["line_width"] = width
    if "point_size" in spec:
        applied["point_size"] = int(spec["point_size"])
        view.PointSize = applied["point_size"]
    if "display_mode" in spec:
        applied["display_mode"] = _parse_enum(
            view, "DisplayMode", spec["display_mode"], "display_mode"
        )
        view.DisplayMode = applied["display_mode"]
    if "draw_style" in spec:
        applied["draw_style"] = _parse_enum(view, "DrawStyle", spec["draw_style"], "draw_style")
        view.DrawStyle = applied["draw_style"]
    if "visible" in spec:
        if not isinstance(spec["visible"], bool):
            raise ValueError(f"visible must be true or false, got {spec['visible']!r}.")
        view.Visibility = spec["visible"]
        applied["visible"] = spec["visible"]

    if "color" in spec:
        # Read it back off the view provider: a color that silently did not stick
        # must not read as a successful repaint.
        back = tuple(float(c) for c in view.ShapeColor)
        wanted = parse_color(spec["color"])
        if max(abs(back[i] - wanted[i]) for i in range(3)) > 0.02:
            raise RuntimeError(
                f"color did not persist on '{obj.Name}': asked for "
                f"{format_color(wanted)}, the view provider reports {format_color(back)}."
            )

    entries = list(getattr(view, "ShapeAppearance", ()) or ())
    if len(entries) > 1:
        # A per-face material list is NOT repainted by a property write: live,
        # two colour writes on a 3-entry object left the view drawing the FIRST
        # one (the stored materials were green, the render stayed blue), because
        # the node's material array is only rebuilt by an actual ShapeAppearance
        # assignment. Collapsing to ONE uniform entry is this op's semantics
        # anyway (a whole-object appearance), so do it and say so.
        view.ShapeAppearance = (
            _fresh_material(
                entries[0],
                diffuse=parse_color(spec["color"]) if "color" in spec else None,
                transparency=(
                    applied["transparency"] / 100.0 if "transparency" in applied else None
                ),
            ),
        )
        applied["normalized_appearance"] = f"{len(entries)} materials -> 1 uniform"
    return {"object": obj.Name, **applied}


def _build_color(doc, spec):
    """Set appearance (color/transparency/line/display) on one or more objects.

    Spec keys (obj_properties): color, transparency, line_color, line_width,
    point_size, display_mode, draw_style, visible — see operation_help("color").
    Colors accept 0..1 floats, 0-255 ints, "#rrggbb" or a name.
    """
    if not [k for k in _APPEARANCE_KEYS if k in spec]:
        raise ValueError(
            "color needs at least one appearance key in obj_properties: "
            + ", ".join(_APPEARANCE_KEYS)
            + "."
        )
    colored = []
    skipped = []
    for obj, note, explicit in _color_targets(doc, spec):
        try:
            entry = _apply_appearance(obj, spec)
        except Exception as e:
            if explicit:
                raise
            # A wildcard sweep meets objects that cannot be painted at all (a
            # Spreadsheet's view provider has no ShapeColor, and `variables` puts
            # one in every parametric document). Failing the whole sweep over one
            # of them would make obj_name="*" useless on exactly the models worth
            # coloring — but the caller must still be told which ones it missed.
            skipped.append({"object": obj.Name, "reason": f"{type(e).__name__}: {e}"})
            continue
        if note:
            entry["note"] = note
        colored.append(entry)
    if not colored:
        raise RuntimeError(
            "color matched no object it could paint: "
            + "; ".join(f"{s['object']} ({s['reason']})" for s in skipped[:4])
        )

    # `global` is load-bearing: without it this assignment binds a LOCAL and
    # describe_feature's pop finds nothing, so the reply's `colored` readback
    # came back empty on every call (live-caught).
    global _LAST_FEATURE_INFO
    _LAST_FEATURE_INFO = {"color": colored, "skipped": skipped}
    first = doc.getObject(colored[0]["object"])
    FreeCAD.Console.PrintMessage(
        "CADPilot: appearance applied to "
        + ", ".join(f"{c['object']} ({c.get('color', 'unchanged')})" for c in colored[:6])
        + "\n"
    )
    return first


# --- Sketcher / PartDesign -------------------------------------------------------


def _build_sketch(doc, spec):
    return sketcher_ops.build_sketch_gui(doc, spec)


def _require_closed_profile(sketch, op):
    wires = getattr(sketch.Shape, "Wires", [])
    if not any(w.isClosed() for w in wires):
        raise ValueError(
            f"{op} requires a closed profile; '{sketch.Name}' has no closed wire "
            "(check coincident constraints between endpoint pairs)."
        )


def _profile_sketch(doc, spec, op):
    _require(spec, "base")
    sketch = _get_obj(doc, spec["base"], "profile sketch")
    if sketch.TypeId != "Sketcher::SketchObject":
        raise ValueError(f"'{spec['base']}' is not a sketch (TypeId={sketch.TypeId}).")
    return sketch


_PAD_TYPES = ("length",)  # uptoface & co. intentionally unsupported for now


def _set_length(feat, op, value):
    """Assign Length with the op's parameter named on parse failures.

    FreeCAD's own messages for a bad quantity name nothing ("syntax error",
    "wrong type as quantity: NoneType"), so the caller could not tell which
    parameter was wrong. Negative values are legal in FreeCAD (the feature
    goes the other way) and only warn via describe_feature.
    """
    try:
        _set_or_bind(feat, "Length", value)
    except Exception as e:
        raise ValueError(
            f"{op} length {value!r} is not a valid length: {e}. Use a number in mm "
            "(7.5 or '10mm') or an expression like '=Vars.thickness'."
        ) from None


def _padlike_attachment_base(body, sketch):
    """The solid an attached profile cuts/fuses into, seen through the chain.

    PartDesign fuses a pocket/pad with the solid its sketch is DIRECTLY
    attached to, but not through an intermediary: a sketch attached to a datum
    plane that is itself attached to a solid gave the pocket no material at
    all, so it extruded its profile as a floating disk and reported success
    (live-verified: body volume 942.5 mm^3 of nothing instead of a cut). Walk
    the attachment chain and hand the solid to the body as its BaseFeature,
    which is what the GUI's "Base feature" tool does.

    A datum plane/point has a Shape of its own (a face / a vertex) but no
    SOLIDS, and adopting one as BaseFeature is silently refused — so only an
    object that actually carries a solid ends the walk.
    """
    ref = sketch
    for _ in range(4):
        support = list(getattr(ref, "AttachmentSupport", None) or [])
        if not support:
            return None
        obj = support[0][0] if support[0] else None
        if obj is None:
            return None
        shape = getattr(obj, "Shape", None)
        if shape is not None and not shape.isNull() and getattr(shape, "Solids", None):
            owner = body_owner(obj)
            if owner is not None and owner is not body:
                # The material lives in ANOTHER body; linking it as this
                # body's BaseFeature would span two bodies. Report no material
                # (the cut then warns instead of pretending).
                return None
            return obj
        ref = obj
    return None


def _ensure_material_base(doc, body, sketch, op):
    """Give the Body a base solid when its profile's material lives outside.

    No-op for a body that already has material, for a plain un-attached
    profile, and when the resolved solid is already a member.
    """
    try:
        if body is None or (body.Shape is not None and not body.Shape.isNull()):
            return None
        if getattr(body, "BaseFeature", None) is not None:
            return None
        solid = _padlike_attachment_base(body, sketch)
        if solid is None or solid in list(getattr(body, "Group", [])):
            return None
        body.BaseFeature = solid
        doc.recompute()
        FreeCAD.Console.PrintMessage(
            f"CADPilot: {op} profile is attached to '{solid.Name}' through a datum; "
            f"adopted it as {body.Name}'s BaseFeature so the feature has material.\n"
        )
        return solid
    except Exception as e:
        FreeCAD.Console.PrintWarning(f"CADPilot: could not adopt a base feature: {e}\n")
        return None


def _build_padlike(doc, spec, fc_type, default_name):
    body = sketcher_ops._get_or_create_body(doc, spec.get("body"))
    sketch = _profile_sketch(doc, spec, fc_type)
    ptype = str(spec.get("pad_type", "length")).lower()
    if ptype not in _PAD_TYPES:
        raise ValueError(
            f"pad_type must be one of {_PAD_TYPES}, got {ptype!r} — for a parametric "
            "through cut pass through_all=true instead."
        )
    doc.recompute()
    _require_closed_profile(sketch, fc_type)
    _ensure_material_base(doc, body, sketch, fc_type.split("::")[-1].lower())
    feat = body.newObject(fc_type, spec.get("name") or default_name)
    feat.Profile = sketch
    if spec.get("through_all"):
        # PartDesign's OWN parametric through-all, not a big numeric length.
        # `through_all` used to be ignored here, so the pocket silently took
        # `length`'s default 10 mm: the hole looked through on a plate thinner
        # than 10 and quietly grew a floor the moment the model passed it (live:
        # a Ø6 "through" hole in a flange was exact at 8 mm, then left a 2 mm
        # floor at 12 mm and a 10 mm floor at 20 mm — +1696 mm³ of uncut material
        # and 6 extra bottom faces, with the volume formula the only clue).
        # `length` is ignored while this is set, by FreeCAD's own semantics.
        try:
            feat.Type = "ThroughAll"
        except Exception as e:
            raise ValueError(
                f"through_all is not available on {fc_type}: {e}. Drop it and give length instead."
            ) from None
    else:
        _set_length(feat, fc_type.split("::")[-1].lower(), spec.get("length", 10.0))
    feat.Reversed = bool(spec.get("reversed", False))
    feat.Midplane = bool(spec.get("midplane", False))
    return feat


def _build_pad(doc, spec):
    return _build_padlike(doc, spec, "PartDesign::Pad", "Pad")


def _build_pocket(doc, spec):
    return _build_padlike(doc, spec, "PartDesign::Pocket", "Pocket")


# A recompute failure with an empty StatusString leaves the caller nothing to
# act on; these name the operation's own known trap instead.
_FAILURE_HINTS = {
    # OCC's MakeThickSolid fails on a removed face that carries an INNER WIRE
    # (measured live: a 60x40x10 plate with a 20x20 blind pocket refused
    # thickness("+Z") at value 2 and 3, StatusString empty, while the opposite
    # face worked).
    "thickness": "OCC fails when a removed face carries an inner wire (a hole/pocket); "
    "try reversed=true, a smaller value, or the opposite face",
    "revolution": "check that the profile is a closed wire on ONE side of the axis "
    "(a profile crossing its revolve axis is rejected by FreeCAD)",
    "groove": "check that the profile is a closed wire on one side of the axis",
}

_REV_AXIS = {"X": "H_Axis", "Y": "V_Axis", "Z": "N_Axis"}


def _build_revlike(doc, spec, fc_type, default_name):
    body = sketcher_ops._get_or_create_body(doc, spec.get("body"))
    sketch = _profile_sketch(doc, spec, fc_type)
    doc.recompute()
    _require_closed_profile(sketch, fc_type)
    _ensure_material_base(doc, body, sketch, fc_type.split("::")[-1].lower())
    feat = body.newObject(fc_type, spec.get("name") or default_name)
    feat.Profile = sketch
    _set_or_bind(feat, "Angle", spec.get("angle", 360.0))
    # Default "Y" (the sketch's V axis) is the lathe setup a profile is
    # normally drawn for: v = axial position, u = radius. The old default "Z"
    # was the profile normal, i.e. always degenerate.
    axis = spec.get("axis", "Y")
    edge = axis.get("edge") if isinstance(axis, dict) else None
    if edge:
        if not (isinstance(edge, (list, tuple)) and len(edge) == 2):
            raise ValueError("axis.edge must be [object_name, 'EdgeN'].")
        ref = _get_obj(doc, edge[0], "axis edge object")
        edge_name = str(edge[1])
        n = int(edge_name[4:]) if edge_name.startswith("Edge") else 0
        if n < 1 or n > len(ref.Shape.Edges):
            raise ValueError(
                f"'{edge_name}' out of range on '{ref.Name}' (1-{len(ref.Shape.Edges)})."
            )
        feat.ReferenceAxis = (ref, [edge_name])
    else:
        sub = _REV_AXIS.get(str(axis).upper())
        if sub is None:
            raise ValueError("axis must be 'X'/'Y'/'Z' or {\"edge\": [obj, \"EdgeN\"]}")
        if sub == "N_Axis":
            # Revolving a planar profile about its own normal keeps every
            # point in the drawing plane: the result is a flat zero-volume
            # shell, never a solid (FreeCAD still calls it valid).
            raise ValueError(
                "axis='Z' is the profile's own normal — revolving about it sweeps a "
                "flat zero-volume shell, not a solid. Use 'X'/'Y' (the sketch's "
                'in-plane axes) or {"edge": [obj, "EdgeN"]}.'
            )
        feat.ReferenceAxis = (sketch, [sub])
    feat.Reversed = bool(spec.get("reversed", False))
    return feat


def _build_revolution(doc, spec):
    return _build_revlike(doc, spec, "PartDesign::Revolution", "Revolution")


def _build_groove(doc, spec):
    return _build_revlike(doc, spec, "PartDesign::Groove", "Groove")


_ORIGIN_ROLE = {"XY": "XY_Plane", "XZ": "XZ_Plane", "YZ": "YZ_Plane"}


def _build_datum_plane(doc, spec):
    """PartDesign datum plane on a base plane or existing face (+offset)."""
    _require(spec, "plane")
    body = sketcher_ops._get_or_create_body(doc, spec.get("body"))
    dp = body.newObject("PartDesign::Plane", spec.get("base") or "DatumPlane")
    plane = spec["plane"]
    support_prop = "AttachmentSupport" if hasattr(dp, "AttachmentSupport") else "Support"
    center_target = None
    if isinstance(plane, str):
        role = _ORIGIN_ROLE.get(plane.upper())
        if role is None:
            raise ValueError(f"plane must be XY/XZ/YZ or {{'face': ...}}, got {plane!r}")
        origin = getattr(body, "Origin", None)
        support = None
        for feat in getattr(origin, "OriginFeatures", []):
            if getattr(feat, "Role", "") == role:
                support = feat
                break
        if support is None:
            raise ValueError(f"body '{body.Name}' origin has no {role}.")
        setattr(dp, support_prop, [(support, "")])
    elif isinstance(plane, dict) and plane.get("face"):
        face = plane["face"]
        if not (isinstance(face, (list, tuple)) and len(face) == 2):
            raise ValueError("plane.face must be [object_name, 'FaceN'].")
        ref = _get_obj(doc, face[0], "datum plane face object")
        face_name = str(face[1])
        # Direction tokens (+Z/top/…) resolve like a sketch's plane.face —
        # face names are re-derived after every feature, so the direction is
        # the stable reference.
        if face_name.startswith(("+", "-")) or face_name.lower() in sketcher_ops._FACE_WORDS:
            face_name = sketcher_ops._resolve_semantic_face(ref, face_name)
        n = int(face_name[4:]) if face_name.startswith("Face") else 0
        if n < 1 or n > len(ref.Shape.Faces):
            if not face_name.startswith("Face"):
                raise ValueError(
                    f"plane.face[1] must be a face name ('FaceN', 1-"
                    f"{len(ref.Shape.Faces)}) or a direction token ('+Z', '-X', 'top', "
                    "'bottom', 'front', 'back', 'left', 'right', 'MaxX', ...), got "
                    f"{face_name!r}."
                )
            raise ValueError(
                f"'{face_name}' out of range on '{ref.Name}' (1-{len(ref.Shape.Faces)})."
            )
        setattr(dp, support_prop, [(ref, face_name)])
        _record_resolved_faces(ref, [face_name])
        if plane.get("center"):
            center_target = (ref, face_name)
    else:
        raise ValueError(f"plane must be XY/XZ/YZ or {{'face': ...}}, got {plane!r}")
    dp.MapMode = "FlatFace"
    offset = float(spec.get("offset", 0) or 0)
    if center_target is not None:
        # plane={"face": [...], "center": true}: origin at the middle of the
        # face instead of its parametric origin (a corner on rectangular
        # faces), so a sketch attached to this datum lands where it looks.
        sketcher_ops.center_attachment(dp, center_target[0], center_target[1], doc, offset)
    elif offset:
        dp.AttachmentOffset = FreeCAD.Placement(FreeCAD.Vector(0, 0, offset), FreeCAD.Rotation())
    return dp


def _hull_view_faces(sketch):
    """Closed wires of a view sketch -> planar faces (global coords)."""
    faces = [Part.Face(w) for w in getattr(sketch.Shape, "Wires", []) if w.isClosed()]
    if not faces:
        raise ValueError(f"hull view '{sketch.Name}' has no closed wire.")
    return faces


def _build_hull(doc, spec):
    """Visual hull: intersect the extrusions of 2-3 view-profile sketches.

    Result is a static Part::Feature (no proxy — survives document reload
    without the addon on sys.path). Re-running with the same name replaces
    the Shape in place (iterate: edit a view sketch, re-run hull).

    v1 limits: view sketches must sit at the global origin (their Placement
    IS the global frame); each view contributes its closed wires fused, so
    use ONE outer profile per view (hole wires are not subtracted).
    """
    _require(spec, "sketches")
    names = spec["sketches"]
    if isinstance(names, dict):
        names = [names[k] for k in ("top", "front", "side") if names.get(k)]
    if not isinstance(names, list) or not 2 <= len(names) <= 3:
        raise ValueError("hull requires 2-3 view sketches (list or {'top','front','side'}).")
    views = []
    mins = [float("inf")] * 3
    maxs = [float("-inf")] * 3
    for n in names:
        s = _get_obj(doc, n, "hull view sketch")
        if s.TypeId != "Sketcher::SketchObject":
            raise ValueError(f"'{n}' is not a sketch (TypeId={s.TypeId}).")
        faces = _hull_view_faces(s)
        for f in faces:
            bb = f.BoundBox
            mins = [min(mins[0], bb.XMin), min(mins[1], bb.YMin), min(mins[2], bb.ZMin)]
            maxs = [max(maxs[0], bb.XMax), max(maxs[1], bb.YMax), max(maxs[2], bb.ZMax)]
        views.append((s, faces))

    diag = (FreeCAD.Vector(*maxs) - FreeCAD.Vector(*mins)).Length
    margin = float(spec.get("margin") or max(1.0, 0.05 * diag))
    corners = [
        FreeCAD.Vector(x, y, z)
        for x in (mins[0], maxs[0])
        for y in (mins[1], maxs[1])
        for z in (mins[2], maxs[2])
    ]

    prisms = []
    for s, faces in views:
        normal = s.Placement.Rotation.multVec(FreeCAD.Vector(0, 0, 1))
        dots = [c.dot(normal) for c in corners]
        tmin, tmax = min(dots) - margin, max(dots) + margin
        prism = None
        for f in faces:
            fc = f.copy()  # translate() is in-place; never mutate sketch geometry
            fc.translate(normal * (tmin - f.CenterOfMass.dot(normal)))
            e = fc.extrude(normal * (tmax - tmin))
            prism = e if prism is None else prism.fuse(e)
        prisms.append(prism)

    result = prisms[0]
    for p in prisms[1:]:
        result = result.common(p)
    if not result.Solids or result.Volume < 1e-6:
        raise RuntimeError(
            "visual hull is empty — the view profiles do not overlap along every axis."
        )
    if len(result.Solids) > 1:
        result = max(result.Solids, key=lambda sol: sol.Volume)

    name = spec.get("base") or "Hull"
    existing = doc.getObject(name)
    if existing is not None:
        if existing.TypeId != "Part::Feature":
            raise ValueError(
                f"'{name}' exists and is not a hull result (TypeId={existing.TypeId})."
            )
        existing.Shape = result
        return existing
    feat = doc.addObject("Part::Feature", name)
    feat.Shape = result
    return feat


def _build_thickness(doc, spec):
    _require(spec, "faces", "value")
    if spec["faces"] == "all":
        # faces = the faces to OPEN. Selecting all of them leaves nothing to
        # remove: FreeCAD accepts the feature and returns the solid unchanged,
        # so the old path reported success with identical geometry (live).
        raise ValueError(
            "thickness needs the faces to OPEN; faces='all' removes every wall and "
            "FreeCAD returns the solid unchanged. List the faces to remove "
            '(e.g. faces=[5] or ["Face5"]) or all but one.'
        )
    body = sketcher_ops._get_or_create_body(doc, spec.get("body"))
    base = _get_obj(doc, spec["base"], "base feature")
    owner = _parent_body(base)
    if owner is not None:
        _require_end_of_chain(owner, base, "thickness")
    names = _resolve_elements(base, spec["faces"], "Face")
    _record_resolved_faces(base, names)
    feat = body.newObject("PartDesign::Thickness", spec.get("name") or "Thickness")
    if "Faces" in feat.PropertiesList:
        # FreeCAD <= 1.0: plain Base link + separate Faces LinkSub.
        feat.Base = base
        feat.Faces = (base, names)
    else:
        # FreeCAD >= 1.1 rework: faces ride along on the Base LinkSub.
        feat.Base = (base, names)
    _set_or_bind(feat, "Value", spec["value"])
    feat.Reversed = bool(spec.get("reversed", False))
    return feat


def _build_draft(doc, spec):
    _require(spec, "faces", "angle")
    body = sketcher_ops._get_or_create_body(doc, spec.get("body"))
    base = _get_obj(doc, spec["base"], "base feature")
    owner = _parent_body(base)
    if owner is not None:
        _require_end_of_chain(owner, base, "draft")
    names = _resolve_elements(base, spec["faces"], "Face")
    _record_resolved_faces(base, names)
    neutral = spec.get("neutral_plane", names[0])
    neutral_names = _resolve_elements(
        base, neutral if isinstance(neutral, list) else [neutral], "Face"
    )
    feat = body.newObject("PartDesign::Draft", spec.get("name") or "Draft")
    if "Faces" in feat.PropertiesList:
        feat.Base = base
        feat.Faces = (base, names)
    else:
        feat.Base = (base, names)
    _set_or_bind(feat, "Angle", spec["angle"])
    feat.NeutralPlane = (base, neutral_names[:1])
    direction = spec.get("pull_direction")
    if direction is not None:
        # PullDirection is a LinkSub: {"edge": ["ObjName", "EdgeN"]} —
        # the draft pulls along that edge's direction.
        edge = direction.get("edge") if isinstance(direction, dict) else None
        if not (isinstance(edge, (list, tuple)) and len(edge) == 2):
            raise ValueError(
                'pull_direction must be {"edge": [obj, "EdgeN"]} '
                "(or omitted to keep the FreeCAD default)."
            )
        ref = _get_obj(doc, edge[0], "pull direction edge object")
        edge_name = str(edge[1])
        n = int(edge_name[4:]) if edge_name.startswith("Edge") else 0
        if n < 1 or n > len(ref.Shape.Edges):
            raise ValueError(
                f"'{edge_name}' out of range on '{ref.Name}' (1-{len(ref.Shape.Edges)})."
            )
        feat.PullDirection = (ref, [edge_name])
    return feat


_CUT_TYPES = ("pocket", "groove")


def _cut_without_material(feat) -> str:
    """Warn when a cut feature has no solid to cut into.

    A pocket/groove whose profile resolves to no material (an un-attached
    sketch in an empty body, an attachment chain ending in another body) still
    "succeeds": FreeCAD returns the profile's own extrusion — a floating disk
    where the user expected a hole (live: 942.5 mm^3 of nothing). The material
    must come from ANOTHER member: reading `body.Shape` alone is not enough,
    because by the time this runs the body's tip IS the cut feature, so its
    own extrusion looked like material and the warning went silent
    (live-verified: an unsupported sketch in an empty body reported plain
    success).
    """
    try:
        if getattr(feat, "BaseFeature", None) is not None:
            return ""
        body = _parent_body(feat)
        if body is not None:
            adopted = getattr(body, "BaseFeature", None)
            if adopted is not None:
                sh = getattr(adopted, "Shape", None)
                if sh is not None and not sh.isNull() and float(sh.Volume) > 0:
                    return ""  # an adopted base solid is the material
            for member in getattr(body, "Group", []) or []:
                if member is feat:
                    continue
                if not tip_policy.advances_tip(getattr(member, "TypeId", "")):
                    continue
                sh = getattr(member, "Shape", None)
                if sh is not None and not sh.isNull() and float(sh.Volume) > 0:
                    return ""  # attachment fusion with an existing member
        return (
            f"{feat.Name} has no material to cut: neither the body nor a BaseFeature "
            "provides a solid, so the result is the profile's own extrusion (a floating "
            "disk), not a hole. Attach the profile to a face of the solid this body "
            "contains, or give the body its base first."
        )
    except Exception:
        return ""


def _cut_removed_nothing(feat) -> str:
    """Warn when a cut feature removed no material.

    A pocket attached to the wrong face (or whose profile sits outside the
    solid) cuts air and still reports success — the sketch lands on a bad plane
    silently. The ground truth is the body's volume just before this feature:
    a PartDesign feature's ``BaseFeature`` is its predecessor in the body.
    """
    try:
        baseline = getattr(feat, "BaseFeature", None)
        if baseline is None:
            profile = feat.Profile[0]
            support = list(getattr(profile, "AttachmentSupport", None) or [])
            baseline = support[0][0] if support else None
        if baseline is None:
            return ""
        prev_vol = float(baseline.Shape.Volume)
        new_vol = float(feat.Shape.Volume)
    except Exception:
        return ""
    if prev_vol <= 0 or new_vol < prev_vol - 1e-6:
        return ""
    return (
        f"{feat.Name} removed no material (volume unchanged at {round(new_vol, 1)} mm^3): "
        "the profile is probably outside the solid or the sketch attached to the wrong "
        "face. Face names shift after every feature — prefer a direction selector, "
        "plane={'face': [obj, '+Z']}."
    )


def _pad_created_disconnected_solid(feat) -> str:
    """Warn when an additive feature left the Body with a floating solid.

    A PartDesign Body has AllowCompound=true by default, so a pad whose
    profile does not touch the base still "succeeds" — the Body quietly
    becomes a two-solid compound (live: a 750 mm^3 slab floating 15 mm off
    the base, counted into the Body's volume, no error anywhere). A solid
    count that grew past the predecessor's is the signal.
    """
    try:
        baseline = getattr(feat, "BaseFeature", None)
        if baseline is None:
            profile = feat.Profile[0] if getattr(feat, "Profile", None) else None
            support = list(getattr(profile, "AttachmentSupport", None) or [])
            baseline = support[0][0] if support else None
        if baseline is None:
            return ""
        before = len(baseline.Shape.Solids)
        after = len(feat.Shape.Solids)
    except Exception:
        return ""
    if after <= max(before, 1):
        return ""
    return (
        f"{feat.Name} produced {after} disconnected solid(s) (the base had {before}): the "
        "profile does not touch the base, so the pad floats next to it (FreeCAD bodies "
        "allow compounds, so this is valid but almost never intended). Check the sketch's "
        "attachment and its position on the face."
    )


def _thickness_changed_nothing(feat) -> str:
    """Warn when a shell left the volume unchanged.

    The remaining case is a selection that spans the whole surface (or a body
    whose walls cannot be offset): FreeCAD computes, returns the base solid,
    and the op would report plain success with identical geometry.
    """
    try:
        baseline = getattr(feat, "Base", None)
        if isinstance(baseline, tuple):
            baseline = baseline[0]
        if baseline is None:
            baseline = getattr(feat, "BaseFeature", None)
        if baseline is None:
            return ""
        prev = float(baseline.Shape.Volume)
        new = float(feat.Shape.Volume)
    except Exception:
        return ""
    if prev <= 0 or abs(new - prev) > max(1e-6, prev * 1e-9):
        return ""
    return (
        f"{feat.Name} changed nothing (volume still {round(new, 1)} mm^3): the selected "
        "faces leave no wall to create. For a shell, select the faces to OPEN — a "
        "selection covering the whole surface removes nothing."
    )


def _thickness_grew_outward(feat) -> str:
    """Warn when the shell ADDED material outside the original part.

    PartDesign's Thickness default offsets outward whenever the selected face
    normal points out of the material (live: a 100x60x40 box came back
    -3..103 in every axis, 240000 -> 59850 mm^3 with the walls sitting on the
    OUTSIDE). "Shell" usually means hollowing the part, so the growth is worth
    naming; reversed=true flips the direction.
    """
    try:
        baseline = getattr(feat, "Base", None)
        if isinstance(baseline, tuple):
            baseline = baseline[0]
        if baseline is None:
            baseline = getattr(feat, "BaseFeature", None)
        if baseline is None:
            return ""
        b0 = baseline.Shape.BoundBox
        b1 = feat.Shape.BoundBox
    except Exception:
        return ""
    grew = (
        b1.XMin < b0.XMin - 1e-6
        or b1.YMin < b0.YMin - 1e-6
        or b1.ZMin < b0.ZMin - 1e-6
        or b1.XMax > b0.XMax + 1e-6
        or b1.YMax > b0.YMax + 1e-6
        or b1.ZMax > b0.ZMax + 1e-6
    )
    if not grew:
        return ""
    return (
        f"{feat.Name} added material OUTSIDE the base part (bbox grew from "
        f"[{round(b0.XMin, 1)}, {round(b0.YMin, 1)}, {round(b0.ZMin, 1)}] x "
        f"[{round(b0.XMax, 1)}, {round(b0.YMax, 1)}, {round(b0.ZMax, 1)}] to "
        f"[{round(b1.XMin, 1)}, {round(b1.YMin, 1)}, {round(b1.ZMin, 1)}] x "
        f"[{round(b1.XMax, 1)}, {round(b1.YMax, 1)}, {round(b1.ZMax, 1)}]): the "
        "selected face's normal points out of the material, so the wall was offset "
        "outward. Pass reversed=true for an inward (hollowing) shell."
    )


def describe_feature_reply(feat, spec) -> dict:
    """``describe_feature`` plus the shape-check advisory — the RPC layer's call.

    The advisory belongs to the op that JUST ran, so it is merged here rather
    than in every branch of describe_feature (whose body keeps its own, widely
    asserted, shape)."""
    out = describe_feature(feat, spec) or {}
    note = _take_shape_check_note()
    if note:
        warnings = out.get("warnings")
        if isinstance(warnings, list):
            warnings.append(note)
        else:
            out["warnings"] = [note]
    return out


def describe_feature(feat, spec) -> dict:
    """Extra result fields for the RPC response (sketch, hull, cut no-ops)."""
    op = spec.get("type")
    if op == "hull":
        sh = feat.Shape
        return {"volume_mm3": round(sh.Volume, 2), "solids": len(sh.Solids)}
    if op == "color":
        # One entry per object actually painted, with the values READ BACK off
        # the ViewObject: the reply is the only place a multi-object or
        # redirected (feature -> Body) call becomes visible, and `skipped` is the
        # only place a wildcard sweep says what it could not paint. Both lists
        # are capped (the counts always ride along) so a whole-document sweep
        # cannot dump hundreds of rows into the caller's context.
        info = pop_last_feature_info() or {}
        colored = info.get("color") or []
        skipped = info.get("skipped") or []
        out = {"colored_count": len(colored), "colored": colored[:_COLOR_REPORT_MAX]}
        if len(colored) > _COLOR_REPORT_MAX:
            out["colored_truncated"] = len(colored) - _COLOR_REPORT_MAX
        if skipped:
            out["skipped_count"] = len(skipped)
            out["skipped"] = skipped[:_COLOR_REPORT_MAX]
            names = ", ".join(s["object"] for s in skipped[:5])
            out["warnings"] = [
                f"color skipped {len(skipped)} object(s) it cannot paint: {names} "
                f"— first reason: {skipped[0]['reason']}"
            ]
        return out
    if op in ("fillet", "chamfer"):
        info = pop_last_feature_info() or {}
        if info.get("dressed"):
            # A Body was named: name the feature that was dressed, or "which
            # feature is the Body's Tip" stays a guess.
            return {
                "dressed_object": info["dressed"],
                "note": f"the {op} was built on '{info['dressed']}', the Tip of Body "
                f"'{info['body']}' (a PartDesign dress-up lives inside its Body and "
                "becomes its new Tip).",
            }
        if info.get("hidden_base"):
            return {
                "hidden_base": info["hidden_base"],
                "note": f"the {op} replaced '{info['hidden_base']}' in the display: the "
                "base object is the same solid with sharper edges, so its Visibility was "
                "turned off to avoid two overlapping solids. Its data is untouched; set "
                "Visibility back on if you need to see it.",
            }
        return {}
    warnings: list[str] = []
    resolved = pop_last_feature_info() if op in ("thickness", "draft", "datum_plane") else None
    if op in _CUT_TYPES:
        message = _cut_without_material(feat) or _cut_removed_nothing(feat)
        if message:
            warnings.append(message)
    if op in ("pad", "pocket"):
        length = spec.get("length")
        if isinstance(length, (int, float)) and length < 0:
            warnings.append(
                f"{feat.Name} length {length} is negative: FreeCAD reads that as the "
                "opposite direction (it is not an error), and the geometry went that way."
            )
    if op == "pad":
        message = _pad_created_disconnected_solid(feat)
        if message:
            warnings.append(message)
    if op == "thickness":
        for helper in (_thickness_changed_nothing, _thickness_grew_outward):
            message = helper(feat)
            if message:
                warnings.append(message)
    if op == "move" and spec.get("base") and feat is not None and spec["base"] != feat.Name:
        # The move went to the owning Body (a feature's Placement is reset by
        # every recompute) — say so, or the redirect looks like the wrong
        # object was moved.
        return {
            "moved_object": feat.Name,
            "note": (
                f"'{spec['base']}' is a PartDesign feature: FreeCAD resets a feature's "
                f"Placement on the next recompute, so the move was applied to its Body "
                f"'{feat.Name}'."
            ),
        }
    if op != "sketch":
        out = {"warnings": warnings} if warnings else {}
        if resolved:
            # Which faces the selector resolved to (center + normal): a
            # direction token picks the farthest face facing that way, so a
            # surprise pick must be visible in the reply.
            out["resolved"] = resolved
        return out
    info = sketcher_ops.pop_last_sketch_info() or {}
    out = {k: v for k, v in info.items() if k != "object_name"}
    if info.get("fully_constrained") is False:
        warnings.append(
            f"sketch '{feat.Name}' is under-constrained (DoF={info.get('dof')}); "
            "add dimensional/geometric constraints to fully define it."
        )
    if warnings:
        out["warnings"] = warnings
    return out


_BUILDERS = {
    "boolean": _build_boolean,
    "fillet": lambda doc, spec: _build_fillet_chamfer(doc, spec, "fillet"),
    "chamfer": lambda doc, spec: _build_fillet_chamfer(doc, spec, "chamfer"),
    "loft": _build_loft,
    "sweep": _build_sweep,
    "mirror": _build_mirror,
    "pattern": _build_pattern,
    "move": _build_move,
    "color": _build_color,
    "variables": _build_variables,
    "sketch": _build_sketch,
    "pad": _build_pad,
    "pocket": _build_pocket,
    "revolution": _build_revolution,
    "groove": _build_groove,
    "thickness": _build_thickness,
    "draft": _build_draft,
    "datum_plane": _build_datum_plane,
    "hull": _build_hull,
}


def _advance_body_tip(feat) -> bool:
    """Make ``feat`` its Body's Tip. Returns True when the tip actually moved.

    FreeCAD advances Body.Tip by itself for additive/subtractive features, but
    NOT for a PartDesign transform. Measured on 1.1.4: a polar pattern built from
    a flange pocket was correct on its own (its Shape was the 6-hole result,
    15246 mm^3) while Body.Tip stayed on the single-hole pocket, so the Body kept
    ONE hole (15874 mm^3) and the op reported success.

    Only a successor of the current tip may claim it: Body.Tip is what the body
    SHOWS, so a dress-up on a feature in the middle of the chain taking the tip
    would silently hide every later feature. ``OutList`` — FreeCAD's own "what do
    I link to" — is the honest test for "the tip is my predecessor". A body-less
    feature (a Part-level build) is left alone.
    """
    type_id = getattr(feat, "TypeId", "")
    if not tip_policy.advances_tip(type_id):
        return False
    body = _parent_body(feat)
    if body is None:
        return False
    tip = getattr(body, "Tip", None)
    depends_on_tip = tip is not None and any(o is tip for o in getattr(feat, "OutList", []))
    if not tip_policy.should_advance(
        type_id,
        body_has_tip=tip is not None,
        tip_is_feat=tip is feat,
        feat_depends_on_tip=depends_on_tip,
    ):
        return False
    try:
        body.Tip = feat
    except Exception as exc:
        # A stale tip merely hides the feature; an exception here would discard
        # an otherwise valid build, so warn instead of failing the op.
        FreeCAD.Console.PrintWarning(
            f"CADPilot: could not set {body.Name}.Tip = {feat.Name}: {exc}\n"
        )
        return False
    return True


def _status_string(feat) -> str:
    """FreeCAD's own failure reason for a feature, '' when it has none.

    1.1.4 exposes this as the METHOD ``getStatusString()``; the old
    ``getattr(feat, "StatusString", "")`` silently returned nothing (the
    property does not exist), so every recompute failure lost its reason —
    "Revolve axis intersects the sketch" reached the caller as a bare
    "check parameters/geometry".
    """
    for getter in (
        lambda: feat.getStatusString(),
        lambda: getattr(feat, "StatusString", ""),
    ):
        try:
            status = str(getter() or "").strip()
        except Exception:
            continue
        if status and status != "Invalid":
            return status
    return ""


#: Context of the LAST dress-up (fillet/chamfer) built, for the failure
#: diagnosis below: the kind, the size, the resolved edge names and a probe that
#: re-points the feature at a SUBSET of them and reports whether the result has
#: a usable shape (:func:`_dress_failure_detail`).
_DRESS_CONTEXT: dict[str, Any] = {}


def _remember_dress_context(kind, size, names, probe) -> None:
    _DRESS_CONTEXT.clear()
    _DRESS_CONTEXT.update({"kind": kind, "size": size, "names": list(names), "probe": probe})


def _settle_shape_caches(doc, spec) -> None:
    """Materialise the Shape of every object a feature is about to be built on.

    FreeCAD builds a PartDesign shape LAZILY, and a feature whose predecessor's
    Shape has not been read computes to a Touched/Null shape with no error.
    Live-verified on 1.1.4: after reopening a saved model, re-running an
    identical pocket (the same params that had built fine) produced NO shape at
    all, and reading the SKETCH's Shape first made the same build return exactly
    the analytic result (58800 -> 57207.2 mm^3, one through hole). A recompute is
    not enough; the READ is what performs the build, so the op performs it here.
    """
    if not isinstance(spec, dict):
        return
    targets = []
    for value in (spec.get("base"), spec.get("profile"), spec.get("path"), spec.get("tool")):
        if isinstance(value, str) and value:
            with contextlib.suppress(Exception):
                targets.append(_get_obj(doc, value, "link"))
    profiles = spec.get("profiles")
    if isinstance(profiles, list):
        for value in profiles:
            if isinstance(value, str) and value:
                with contextlib.suppress(Exception):
                    targets.append(_get_obj(doc, value, "profile"))
    body = _parent_body(targets[0]) if targets else None
    tip = getattr(body, "Tip", None) if body is not None else None
    if tip is not None:
        targets.append(tip)  # the feature's real baseline inside a Body
    for obj in targets:
        with contextlib.suppress(Exception):
            _ = obj.Shape
    with contextlib.suppress(Exception):
        doc.recompute()


def _read_shape_state(feat, doc):
    """(shape, valid, volume, solids, error) for a freshly built feature.

    Retried ONCE on failure, because a PartDesign shape builds LAZILY: the first
    access after a recompute can raise FreeCAD's bare "shape is invalid" for a
    shape that reads perfectly well on the second attempt. Measured on 1.1.4 —
    an ordinary 6-rim fillet reported exactly those two words as its whole
    failure, the same call succeeded on the next try with identical inputs, and
    every single rim was valid; the retry is the difference. The volume/solids
    probes belong INSIDE this guard for the same reason (they raise first).
    """
    shape, valid, volume, solids, error = None, False, 0.0, 0, ""
    for attempt in (1, 2):
        try:
            shape = getattr(feat, "Shape", None)
            valid = True if shape is None or shape.isNull() else shape.isValid()
            volume = float(getattr(shape, "Volume", 0.0) or 0.0) if shape is not None else 0.0
            solids = len(getattr(shape, "Solids", []) or []) if shape is not None else 0
            return shape, valid, volume, solids, ""
        except Exception as e:
            error = f"the validity check could not run ({type(e).__name__}: {e})"
            shape, valid, volume, solids = None, False, 0.0, 0
            if attempt == 1:
                with contextlib.suppress(Exception):
                    doc.recompute()
    return shape, valid, volume, solids, error


def _dress_shape_usable(feat) -> bool:
    """Does a dress-up probe have a readable, non-null Shape?

    Null/raising IS the failure being diagnosed; OCC's ``isValid`` is
    deliberately not consulted (it is a false negative on ordinary PartDesign
    fillets — see _SHAPE_CHECK_NOTE). The read is retried once, like every other
    shape read (see _read_shape_state).
    """
    _shape, _valid, _volume, _solids, error = _read_shape_state(feat, feat.Document)
    return not error and _shape is not None and not _shape.isNull()


def _dress_retry_repair() -> bool:
    """Re-apply the SAME edge set once and report whether it now works.

    A dress-up that comes out with a Null Shape is not always a geometry
    verdict: live on 1.1.4, the six bore rims of a patterned plate failed on one
    attempt and produced exactly the 6x roundover (36778.9 mm^3 = 36787.3 -
    6 x 1.393) on the next, with identical inputs — a stale recompute in the
    document's graph is enough. Failing a caller's step over that is wrong, so
    the op retries in place (the Base assignment is idempotent) and only reports
    a failure when the retry fails too.
    """
    probe = _DRESS_CONTEXT.get("probe")
    names = list(_DRESS_CONTEXT.get("names") or [])
    if not callable(probe) or not names:
        return False
    try:
        return bool(probe(names))
    except Exception:
        return False


def _dress_failure_detail(op: str = "") -> str:
    """Test a failed fillet/chamfer's edges ONE at a time and say what happens.

    A multi-edge dress-up that OCC refuses reports nothing usable — live on
    1.1.4, a 0.5 mm fillet on the six bore rims of a patterned plate failed with
    a bare "shape is invalid", while every single rim was fine on its own (and
    so was the top rims two at a time: only the bottom rims fail as a SET).
    "Invalid" cannot be acted on; "split these edges across several fillet
    calls" can. The retries run inside the op's transaction, which aborts when
    the op raises, so the half-built feature is never left behind.
    """
    ctx = dict(_DRESS_CONTEXT)
    _DRESS_CONTEXT.clear()
    if op not in ("fillet", "chamfer"):
        # A stale context from an EARLIER dress-up must never decorate an
        # unrelated failure: live-caught, a pocket that could not compute
        # reported "every one of the 4 edges fails at 4.0 mm on its own".
        return ""
    probe = ctx.get("probe")
    names = list(ctx.get("names") or [])
    kind, size = ctx.get("kind"), ctx.get("size")
    if not callable(probe) or len(names) < 2 or len(names) > 12:
        return ""  # one recompute per edge: not worth it on a large edge set
    bad: list[str] = []
    for name in names:
        try:
            ok = probe([name])
        except Exception:
            ok = False
        if not ok:
            bad.append(name)
    with contextlib.suppress(Exception):
        probe(names)  # hand the full set back, so the caller sees its own failure
    if not bad:
        return (
            f" Each of the {len(names)} edges {kind}s fine on its own (retried one at a "
            f"time), so OCC refuses the SET: split the edges across several {kind} calls, or "
            f"lower the {kind} size."
        )
    if len(bad) == len(names):
        return (
            f" Every one of the {len(names)} edges fails at {size} mm on its own too (retried "
            f"one at a time), so this is not a combination problem: try a smaller {kind} size, "
            "or check the geometry around those edges with Part > Check geometry."
        )
    return (
        f" {len(bad)} of the {len(names)} edge(s) fail on their own too ({', '.join(bad)}); the "
        f"other {len(names) - len(bad)} succeed alone, so the set fails in combination. "
        f"{kind.capitalize()} the failing edges in a separate call."
    )


def _invalid_shape_error(feat_type, feat, detail: str | None = None) -> str:
    """ "<op> produced an invalid Shape" was raised for a self-intersecting
    profile, a dangling reference and a corrupted dependency graph alike — three
    different causes behind one opaque message. Name the object and carry
    FreeCAD's own reason.

    ``detail`` overrides the reason read off the feature: when the validity
    CHECK itself failed there is no status to quote, and quoting FreeCAD's
    (perfectly healthy) "Valid" produced the self-contradictory "produced an
    invalid Shape — FreeCAD says: Valid" (live: a fillet whose BRepCheck threw).
    """
    status = detail or _status_string(feat)
    reason = f" — FreeCAD says: {status}" if status else ""
    return (
        f"{feat_type} '{feat.Name}' produced an invalid Shape{reason}. "
        "Typical causes: the profile/section is not a closed face or wire, the "
        "profile self-intersects, or it references a deleted object. Select "
        f"'{feat.Name}' in FreeCAD and use Part > Check geometry for the OCC fault."
    )


# A feature whose recompute SUCCEEDED, whose FreeCAD status is "Valid" and whose
# shape measures correctly, but whose OCC BRepCheck reports it invalid. That
# combination is a FALSE NEGATIVE of isValid() — refused live for a legitimate
# 2 mm PartDesign fillet on a patterned flange (volume exactly the analytic
# roundover, status Valid, isValid() False) — so it is downgraded from a hard
# failure to a warning the caller can see. Read once, by describe_feature.
_SHAPE_CHECK_NOTE = ""


def _take_shape_check_note() -> str:
    global _SHAPE_CHECK_NOTE
    note, _SHAPE_CHECK_NOTE = _SHAPE_CHECK_NOTE, ""
    return note


def _dress_repaired_note(ftype: str, feat) -> str:
    """A dress-up whose first recompute failed and whose retry succeeded.

    Kept rather than raised: the inputs did not change, so the failure was a
    stale recompute, and failing the caller's step over it loses real work.
    """
    return (
        f"{ftype} '{feat.Name}' came out invalid on the first recompute; re-applying the "
        "same edges produced a valid shape, so the step was kept (a stale recompute can do "
        "this — no parameter changed)."
    )


def create_feature_gui(doc, spec):
    """Create one parametric feature from ``spec``; returns the object.

    Raises ValueError/RuntimeError on any failure. Caller wraps this in a
    transaction, so raising means the document stays untouched.
    """
    ftype = spec.get("type")
    builder = _BUILDERS.get(ftype)
    if builder is None:
        raise ValueError(f"unknown feature type {ftype!r}; supported: {', '.join(FEATURE_TYPES)}")
    # Read the Shape of what this feature is built on FIRST: a lazy predecessor
    # makes the new feature compute to nothing, silently (see the helper).
    _settle_shape_caches(doc, spec)
    feat = builder(doc, spec)
    # "move" directly modifies Placement and "color" only the ViewObject —
    # neither builds a feature, so the recompute/validity/volume checks below do
    # not apply. They must not either: a color op has no business failing
    # because the object it decorates is a half-built or invalid leftover.
    if ftype in ("move", "color"):
        return feat
    doc.recompute()
    if _advance_body_tip(feat):
        doc.recompute()
    # A tip advance rebuilds the Body's display node with FreeCAD's DEFAULT
    # material, so a colour set earlier visually vanishes (see
    # _refresh_appearance). Refresh BEFORE the caller reads anything back.
    body = _parent_body(feat)
    if body is not None and getattr(body, "Tip", None) is feat:
        _refresh_appearance(body)
    state = [str(s) for s in getattr(feat, "State", [])]
    # A dress-up can come out Invalid/Null from a STALE recompute alone (see
    # _dress_retry_repair): re-apply the same edges once before believing it.
    dress = ftype in ("fillet", "chamfer")
    repaired = False
    if "Invalid" in state and dress and _dress_retry_repair():
        repaired = True
        state = [str(s) for s in getattr(feat, "State", [])]
    if "Invalid" in state:
        # FreeCAD's actual failure reason (bad support, missing subelement,
        # "Revolve axis intersects the sketch", …) — "check parameters/geometry"
        # alone sends the caller hunting blind. 1.1.4 exposes it as the METHOD
        # getStatusString(); the old property read returned nothing, so every
        # failure lost its reason.
        status = _status_string(feat)
        detail = f" — {status}" if status and status != "Invalid" else ""
        hint = _FAILURE_HINTS.get(ftype, "check parameters/geometry.")
        raise RuntimeError(
            f"{ftype} failed to recompute{detail} ({hint}){_dress_failure_detail(ftype)}"
        )
    global _SHAPE_CHECK_NOTE
    _SHAPE_CHECK_NOTE = _dress_repaired_note(ftype, feat) if repaired else ""
    shape = None
    check_error = ""
    # The read is retried internally (see _read_shape_state): a lazily built
    # PartDesign shape raises FreeCAD's bare "shape is invalid" on its FIRST
    # access and reads fine on the second.
    shape, valid, volume, solids, check_error = _read_shape_state(feat, doc)
    if not valid and not repaired:
        # A feature that reads as unbuildable may simply have been built on a
        # lazy predecessor: settle those caches and read again before giving up.
        _settle_shape_caches(doc, spec)
        shape, valid, volume, solids, check_error = _read_shape_state(feat, doc)
    if not valid and dress and not repaired and _dress_retry_repair():
        valid = True
        check_error = ""
        shape, valid, volume, solids, _err = _read_shape_state(feat, doc)
        _SHAPE_CHECK_NOTE = _dress_repaired_note(ftype, feat)
    if not valid:
        status = _status_string(feat)
        if check_error or (status and status != "Valid"):
            raise RuntimeError(
                _invalid_shape_error(ftype, feat, check_error or status)
                + _dress_failure_detail(ftype)
            )
        # FreeCAD says Valid, the shape is non-null, the recompute committed and
        # the volume is sane: OCC's BRepCheck disagrees, which it does for good
        # PartDesign fillets (see _SHAPE_CHECK_NOTE). Advisory, not fatal — a
        # false negative that hard-fails a correct model is worse than a warning.
        volume_note = ""
        with contextlib.suppress(Exception):
            volume_note = (
                f" — it holds {len(shape.Solids)} solid(s), volume {float(shape.Volume):.1f} mm^3"
                if shape is not None
                else ""
            )
        _SHAPE_CHECK_NOTE = (
            f"{ftype} '{feat.Name}' recomputed and FreeCAD reports it Valid, but OCC's "
            f"own validity check (Shape.isValid) says otherwise{volume_note}. The "
            "geometry was kept, since that combination is usually a false negative "
            "(it happens on ordinary PartDesign fillets). Verify with Part > Check "
            "geometry if this part matters."
        )
    # A negative volume is never a legitimate solid: OCC returns one for a
    # self-intersecting result (a profile straddling its revolve axis is the
    # classic case) and FreeCAD still calls the feature valid — the caller
    # would get silent garbage. Only shapes that actually HOLD solids are
    # judged: a PartDesign::Plane's infinite face reports a garbage "volume"
    # whose SIGN follows its offset, so an unscoped check refused every
    # datum_plane with a negative offset (live: offset -5 → "volume -1.3e+98").
    if volume < -1e-6 and solids > 0:
        hint = (
            "the profile probably crosses its revolve axis"
            if ftype in ("revolution", "groove")
            else "check the profile/parameters"
        )
        raise RuntimeError(
            f"{ftype} produced a self-intersecting solid (volume {volume:.1f} mm^3 < 0); {hint}."
        )
    # Settle the lazy Shape caches INSIDE this op: a read immediately after the
    # commit (the caller's measure_geometry, the connectivity audit) otherwise
    # saw an interim result — live: a datum-plane pocket reported Body volume
    # 239100.0 for a cut that is exactly 239057.5 once the caches catch up.
    with contextlib.suppress(Exception):
        _ = feat.Shape
        body = _parent_body(feat)
        if body is not None:
            _ = body.Shape
    return feat
