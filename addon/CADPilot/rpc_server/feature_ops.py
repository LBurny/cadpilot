"""Parametric feature creation for the RPC ``create_feature`` handler.

Every feature is a live FreeCAD object (Part::Fillet, Part::Loft, ...)
linked to its source objects, so edits to sources recompute downstream.
Builders raise on any error; the caller (_run_op_with_screenshot) aborts
the transaction, so a failed feature leaves no residue.
"""

import contextlib

import FreeCAD
import Part

from rpc_server import sketcher_ops, tip_policy
from rpc_server.geometry_query import body_owner

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
    seat face landed on a wall top 92 mm above it).
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
                    entry["normal"] = [round(float(v), 4) for v in face.normalAt(0, 0)]
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
    type_map = {"fuse": "Part::Fuse", "cut": "Part::Cut", "common": "Part::Common"}
    fc_type = type_map.get(spec["op"])
    if fc_type is None:
        raise ValueError(f"boolean op must be fuse/cut/common, got {spec['op']!r}")
    feat = doc.addObject(fc_type, spec.get("name") or spec["op"].capitalize())
    feat.Base = _get_obj(doc, spec["base"], "base")
    # Support tool as either a single object name or a list of names.
    tool_val = spec["tool"]
    if isinstance(tool_val, list):
        if not tool_val:
            raise ValueError("boolean tool list must not be empty.")
        if len(tool_val) == 1:
            feat.Tool = _get_obj(doc, tool_val[0], "tool")
        else:
            # Aggregate ALL tools into one compound — Part booleans accept a
            # compound as Tool. (Chaining Part::Fuse pairs would silently drop
            # tools[2:] and leave fuse residue in the tree.)
            tools = [_get_obj(doc, name, "tool") for name in tool_val]
            compound = doc.addObject("Part::Compound", f"{feat.Name}_tools")
            compound.Links = tools
            doc.recompute()
            view = getattr(compound, "ViewObject", None)
            if view is not None:
                view.Visibility = False
            feat.Tool = compound
    else:
        feat.Tool = _get_obj(doc, tool_val, "tool")
    _inherit_appearance(feat, feat.Base)
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
    lives in one, at the document root otherwise.

    A ``Part::Fillet`` is a document-root object — it is not in the Body's
    Group, it does not follow the Body's Placement, and ``Body.Tip = <it>`` is
    accepted silently while leaving the Body ``['Touched', 'Invalid']``. A base
    that is a bare Part-level object still wants the Part::Fillet path.
    """
    size_key = tip_policy.dress_spec_key(kind)
    _require(spec, "base", "edges", size_key)
    base = _get_obj(doc, spec["base"], "base")
    names = _resolve_elements(base, spec["edges"], "Edge")
    size = float(spec[size_key])
    body = _parent_body(base)
    label = spec.get("name") or f"{kind.capitalize()}"
    if body is not None:
        _require_end_of_chain(body, base, kind)
        # FreeCAD 1.1's PartDesign dress-up holds ONE scalar size for all edges
        # (the per-edge tuple form is Part-level only).
        feat = body.newObject(tip_policy.dress_type(kind, True), label)
        feat.Base = (base, names)
        setattr(feat, tip_policy.dress_size_property(kind), size)
        return feat
    feat = doc.addObject(tip_policy.dress_type(kind, False), label)
    if hasattr(feat, "EdgeLinks"):
        # FreeCAD >= 1.1 rework: Base is a plain link and per-edge sizes live
        # in Edges as (1-based edge index, size_start, size_end) tuples.
        feat.Base = base
        feat.Edges = [(int(n[4:]), size, size) for n in names]
    else:
        feat.Base = (base, names)
        setattr(feat, tip_policy.dress_size_property(kind), size)
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
        if datum is not None:
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
            center = spec.get("center", [0, 0, 0])
            angle = float(spec.get("angle", 360.0))
            feat = make_array(base, FreeCAD.Vector(*center), angle, count)
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
    for cell, entry in cells.items():
        if not (isinstance(entry, (list, tuple)) and len(entry) == 2):
            raise ValueError(f"cells['{cell}'] must be [alias, value].")
        alias, value = entry
        if isinstance(value, bool):
            raise ValueError(f"cells['{cell}']: bool is not a valid value.")
        if isinstance(value, (int, float)):
            ss.set(cell, repr(value))
        elif isinstance(value, str):
            ss.set(cell, value if value.startswith("=") else f'"{value}"')
        else:
            raise ValueError(f"cells['{cell}']: unsupported value {value!r}.")
        try:
            ss.setAlias(cell, str(alias))
        except Exception as e:
            # FreeCAD's own "Invalid alias" names neither the cell nor the
            # alias, so the caller could not tell which entry was wrong.
            raise ValueError(
                f"cells['{cell}']: alias {alias!r} was rejected ({e}). Aliases must "
                "start with a letter and contain only letters/digits/underscores "
                "(no spaces, no leading digit, not a cell reference like 'A1')."
            ) from None
    doc.recompute()
    return ss


def _move_vec3(v, name):
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
        new_base = _move_vec3(base, "placement.Base")
        axis = rot_data.get("Axis", {"x": 0, "y": 0, "z": 1})
        new_rot = FreeCAD.Rotation(
            _move_vec3(axis, "placement.Rotation.Axis"),
            float(rot_data.get("Angle", 0)),
        )
        return _assign_placement(doc, obj, new_base, new_rot)

    # Relative translation / rotation — at least one is required.
    translate = spec.get("translate", {})
    rotate = spec.get("rotate", {})
    if not translate and not rotate:
        raise ValueError("move requires at least one of: translate, rotate, placement.")
    delta = _move_vec3(translate, "translate") if translate else FreeCAD.Vector(0, 0, 0)

    # Relative rotation
    delta_rot = FreeCAD.Rotation()
    if rotate:
        if not isinstance(rotate, dict):
            raise ValueError(f"rotate must be a dict {{'axis':…, 'angle':…}}, got {rotate!r}")
        r_axis = _move_vec3(rotate.get("axis", {"x": 0, "y": 0, "z": 1}), "rotate.axis")
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
        raise ValueError(f"pad_type must be one of {_PAD_TYPES}, got {ptype!r}")
    doc.recompute()
    _require_closed_profile(sketch, fc_type)
    _ensure_material_base(doc, body, sketch, fc_type.split("::")[-1].lower())
    feat = body.newObject(fc_type, spec.get("name") or default_name)
    feat.Profile = sketch
    _set_length(feat, fc_type.split("::")[-1].lower(), spec.get("length", 10.0))
    feat.Reversed = bool(spec.get("reversed", False))
    feat.Midplane = bool(spec.get("midplane", False))
    return feat


def _build_pad(doc, spec):
    return _build_padlike(doc, spec, "PartDesign::Pad", "Pad")


def _build_pocket(doc, spec):
    return _build_padlike(doc, spec, "PartDesign::Pocket", "Pocket")


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
    axis = spec.get("axis", "Z")
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

    A pocket/groove whose profile resolves to no material (an attachment chain
    ending in another body, an un-attached sketch in an empty body) still
    "succeeds": FreeCAD returns the profile's own extrusion — a floating disk
    where the user expected a hole (live: 942.5 mm^3 of nothing). The body's
    material or a BaseFeature is what makes a cut real.
    """
    try:
        if getattr(feat, "BaseFeature", None) is not None:
            return ""
        body = _parent_body(feat)
        if body is not None:
            sh = body.Shape
            if sh is not None and not sh.isNull() and float(sh.Volume) > 0:
                return ""  # attachment fusion: the body's material is the base
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


def describe_feature(feat, spec) -> dict:
    """Extra result fields for the RPC response (sketch, hull, cut no-ops)."""
    op = spec.get("type")
    if op == "hull":
        sh = feat.Shape
        return {"volume_mm3": round(sh.Volume, 2), "solids": len(sh.Solids)}
    warnings: list[str] = []
    resolved = pop_last_feature_info() if op in ("thickness", "draft") else None
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


def _invalid_shape_error(feat_type, feat) -> str:
    """ "<op> produced an invalid Shape" was raised for a self-intersecting
    profile, a dangling reference and a corrupted dependency graph alike — three
    different causes behind one opaque message. Name the object and carry
    FreeCAD's own reason."""
    status = str(getattr(feat, "StatusString", "") or "").strip()
    reason = f" — FreeCAD says: {status}" if status and status != "Invalid" else ""
    return (
        f"{feat_type} '{feat.Name}' produced an invalid Shape{reason}. "
        "Typical causes: the profile/section is not a closed face or wire, the "
        "profile self-intersects, or it references a deleted object. Select "
        f"'{feat.Name}' in FreeCAD and use Part > Check geometry for the OCC fault."
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
    feat = builder(doc, spec)
    # "move" directly modifies Placement — no recompute or validity check needed
    if ftype == "move":
        return feat
    doc.recompute()
    if _advance_body_tip(feat):
        doc.recompute()
    state = [str(s) for s in getattr(feat, "State", [])]
    if "Invalid" in state:
        # StatusString carries FreeCAD's actual failure reason (bad support,
        # missing subelement, …) — "check parameters/geometry" alone sends the
        # caller hunting blind.
        status = str(getattr(feat, "StatusString", "") or "").strip()
        detail = f" — {status}" if status and status != "Invalid" else ""
        raise RuntimeError(f"{ftype} failed to recompute{detail} (check parameters/geometry).")
    try:
        shape = getattr(feat, "Shape", None)
        invalid = shape is not None and not shape.isNull() and not shape.isValid()
    except Exception:
        # A feature whose Shape cannot even be read (a broken tip, a dangling
        # link) fails the same way as an invalid one, and must name itself too.
        invalid = True
    if invalid:
        raise RuntimeError(_invalid_shape_error(ftype, feat))
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
