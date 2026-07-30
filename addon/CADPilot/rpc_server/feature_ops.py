"""Parametric feature creation for the RPC ``create_feature`` handler.

Every feature is a live FreeCAD object (Part::Fillet, Part::Loft, ...)
linked to its source objects, so edits to sources recompute downstream.
Builders raise on any error; the caller (_run_op_with_screenshot) aborts
the transaction, so a failed feature leaves no residue.
"""

import FreeCAD
import Part

from rpc_server import sketcher_ops

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


def _resolve_elements(obj, selector, kind):
    """Selector -> sub-element names. kind: 'Edge' or 'Face'."""
    elements = getattr(obj.Shape, "Edges" if kind == "Edge" else "Faces")
    all_names = [f"{kind}{i + 1}" for i in range(len(elements))]
    if selector == "all":
        return all_names
    if not isinstance(selector, list) or not selector:
        raise ValueError(f"selector must be 'all' or a non-empty list, got {selector!r}")
    names = []
    for item in selector:
        if isinstance(item, int):
            if not 0 <= item < len(all_names):
                raise ValueError(f"{kind} index {item} out of range (0-{len(all_names) - 1}).")
            names.append(all_names[item])
        elif isinstance(item, str):
            if item not in all_names:
                raise ValueError(f"'{item}' not a valid sub-element (e.g. {all_names[0]}).")
            names.append(item)
        else:
            raise ValueError(f"selector items must be int or str, got {item!r}")
    return names


def _axis_vec(value, default="Z"):
    v = _AXIS.get(str(value or default).upper())
    if v is None:
        raise ValueError(f"axis must be 'X'/'Y'/'Z', got {value!r}")
    return FreeCAD.Vector(*v)


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
    return feat


def _build_fillet_chamfer(doc, spec, fc_type, size_key):
    _require(spec, "base", "edges", size_key)
    base = _get_obj(doc, spec["base"], "base")
    names = _resolve_elements(base, spec["edges"], "Edge")
    feat = doc.addObject(fc_type, spec.get("name") or fc_type.split("::")[1])
    size = float(spec[size_key])
    if hasattr(feat, "EdgeLinks"):
        # FreeCAD >= 1.1 rework: Base is a plain link and per-edge sizes live
        # in Edges as (1-based edge index, size_start, size_end) tuples.
        feat.Base = base
        feat.Edges = [(int(n[4:]), size, size) for n in names]
    else:
        feat.Base = (base, names)
        setattr(feat, size_key.capitalize(), size)
    return feat


def _build_loft(doc, spec):
    _require(spec, "profiles")
    profiles = [_get_obj(doc, n, "profile") for n in spec["profiles"]]
    if len(profiles) < 2:
        raise ValueError("loft requires at least 2 profiles.")
    feat = doc.addObject("Part::Loft", spec.get("name") or "Loft")
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


def _build_pattern(doc, spec):
    _require(spec, "base", "count")
    import Draft

    make_array = getattr(Draft, "make_array", None) or Draft.makeArray
    base = _get_obj(doc, spec["base"], "base")
    count = int(spec["count"])
    if count < 2:
        raise ValueError("pattern count must be >= 2.")
    ptype = spec.get("pattern_type", "linear")
    if ptype == "linear":
        _require(spec, "spacing")
        direction = _axis_vec(spec.get("axis"), default="X")
        feat = make_array(
            base, direction * float(spec["spacing"]), FreeCAD.Vector(0, 0, 0), count, 1
        )
    elif ptype == "polar":
        center = spec.get("center", [0, 0, 0])
        angle = float(spec.get("angle", 360.0))
        feat = make_array(base, FreeCAD.Vector(*center), angle, count)
        axis = str(spec.get("axis", "Z")).upper()
        if axis != "Z" and hasattr(feat, "Axis"):
            feat.Axis = _axis_vec(axis)
    else:
        raise ValueError(f"pattern_type must be linear/polar, got {ptype!r}")
    if spec.get("name"):
        feat.Label = spec["name"]
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
        ss.setAlias(cell, str(alias))
    doc.recompute()
    return ss


def _build_move(doc, spec):
    """Apply a relative translation and/or rotation to an existing object.

    This is NOT a parametric feature — it directly modifies the object's Placement.
    Supported spec keys:
      translate: {"x": dx, "y": dy, "z": dz}  — relative translation
      rotate: {"axis": {"x":..,"y":..,"z":..}, "angle": degrees}  — relative rotation
      placement: {"Base": {"x":..,"y":..,"z":..}, "Rotation": {...}}  — absolute override
    If both translate/rotate and placement are given, placement wins (absolute).
    """
    _require(spec, "base")
    obj = _get_obj(doc, spec["base"], "base")
    current = obj.Placement

    # Absolute placement override
    if "placement" in spec:
        p = spec["placement"]
        base = p.get("Base", p.get("Position", {"x": 0, "y": 0, "z": 0}))
        rot_data = p.get("Rotation", {"Axis": {"x": 0, "y": 0, "z": 1}, "Angle": 0})
        new_base = FreeCAD.Vector(
            float(base.get("x", 0)), float(base.get("y", 0)), float(base.get("z", 0))
        )
        axis = rot_data.get("Axis", {"x": 0, "y": 0, "z": 1})
        new_rot = FreeCAD.Rotation(
            FreeCAD.Vector(
                float(axis.get("x", 0)), float(axis.get("y", 0)), float(axis.get("z", 0))
            ),
            float(rot_data.get("Angle", 0)),
        )
        obj.Placement = FreeCAD.Placement(new_base, new_rot)
        return obj

    # Relative translation / rotation — at least one is required.
    translate = spec.get("translate", {})
    rotate = spec.get("rotate", {})
    if not translate and not rotate:
        raise ValueError("move requires at least one of: translate, rotate, placement.")
    dx = float(translate.get("x", 0)) if translate else 0
    dy = float(translate.get("y", 0)) if translate else 0
    dz = float(translate.get("z", 0)) if translate else 0
    delta = FreeCAD.Vector(dx, dy, dz)

    # Relative rotation
    delta_rot = FreeCAD.Rotation()
    if rotate:
        r_axis = rotate.get("axis", {"x": 0, "y": 0, "z": 1})
        r_angle = float(rotate.get("angle", 0))  # degrees
        delta_rot = FreeCAD.Rotation(
            FreeCAD.Vector(
                float(r_axis.get("x", 0)), float(r_axis.get("y", 0)), float(r_axis.get("z", 0))
            ),
            r_angle,
        )

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

    obj.Placement = FreeCAD.Placement(new_base, new_rot)
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


def _build_padlike(doc, spec, fc_type, default_name):
    body = sketcher_ops._get_or_create_body(doc, spec.get("body"))
    sketch = _profile_sketch(doc, spec, fc_type)
    ptype = str(spec.get("pad_type", "length")).lower()
    if ptype not in _PAD_TYPES:
        raise ValueError(f"pad_type must be one of {_PAD_TYPES}, got {ptype!r}")
    doc.recompute()
    _require_closed_profile(sketch, fc_type)
    feat = body.newObject(fc_type, spec.get("name") or default_name)
    feat.Profile = sketch
    _set_or_bind(feat, "Length", spec.get("length", 10.0))
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
        setattr(dp, support_prop, [(ref, str(face[1]))])
    else:
        raise ValueError(f"plane must be XY/XZ/YZ or {{'face': ...}}, got {plane!r}")
    dp.MapMode = "FlatFace"
    offset = float(spec.get("offset", 0) or 0)
    if offset:
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
    body = sketcher_ops._get_or_create_body(doc, spec.get("body"))
    base = _get_obj(doc, spec["base"], "base feature")
    names = _resolve_elements(base, spec["faces"], "Face")
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
    names = _resolve_elements(base, spec["faces"], "Face")
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


def describe_feature(feat, spec) -> dict:
    """Extra result fields for the RPC response (sketch and hull)."""
    if spec.get("type") == "hull":
        sh = feat.Shape
        return {"volume_mm3": round(sh.Volume, 2), "solids": len(sh.Solids)}
    if spec.get("type") != "sketch":
        return {}
    info = sketcher_ops.pop_last_sketch_info() or {}
    out = {k: v for k, v in info.items() if k != "object_name"}
    if info.get("fully_constrained") is False:
        out["warnings"] = [
            f"sketch '{feat.Name}' is under-constrained (DoF={info.get('dof')}); "
            "add dimensional/geometric constraints to fully define it."
        ]
    return out


_BUILDERS = {
    "boolean": _build_boolean,
    "fillet": lambda doc, spec: _build_fillet_chamfer(doc, spec, "Part::Fillet", "radius"),
    "chamfer": lambda doc, spec: _build_fillet_chamfer(doc, spec, "Part::Chamfer", "size"),
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
    state = [str(s) for s in getattr(feat, "State", [])]
    if "Invalid" in state:
        raise RuntimeError(f"{ftype} failed to recompute (check parameters/geometry).")
    shape = getattr(feat, "Shape", None)
    if shape is not None and not shape.isNull() and not shape.isValid():
        raise RuntimeError(f"{ftype} produced an invalid Shape.")
    return feat
