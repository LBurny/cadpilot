"""Constrained-sketch construction for the RPC ``sketch`` feature op.

A sketch is built atomically: geometry + constraints are applied in one go,
the solver runs, and the result is validated. Callers wrap this in a document
transaction, so any raised error leaves the document untouched.

Result metadata (DoF, solver diagnostics) is stashed module-level and consumed
by ``feature_ops.describe_feature`` — builders only return the object itself.
"""

import math

import FreeCAD
import Part
import Sketcher

# Result of the most recent build_sketch_gui call; read once by
# describe_feature() via pop_last_sketch_info().
_LAST_SKETCH_INFO: dict | None = None

# spec point keyword -> Sketcher PointPos
_POINT_POS = {"start": 1, "end": 2, "center": 3, "mid": 3}

# Base-plane placements: sketch local +Z is the drawing normal.
# Convention: XZ sketch normal = +Y, YZ sketch normal = +X.
_BASE_PLANES = {
    "XY": ((0, 0, 0), (1, 0, 0), 0),
    "XZ": ((0, 0, 0), (1, 0, 0), -90),
    "YZ": ((0, 0, 0), (0, 1, 0), 90),
}


def pop_last_sketch_info() -> dict | None:
    global _LAST_SKETCH_INFO
    info = _LAST_SKETCH_INFO
    _LAST_SKETCH_INFO = None
    return info


def _get_or_create_body(doc, name: str | None):
    """Resolve the PartDesign Body to build in.

    With ``name``: must exist and be a Body. Without: use the document's only
    Body, create one when absent, and refuse to guess when several exist.
    """
    if name:
        body = doc.getObject(name)
        if body is None:
            raise ValueError(f"Body '{name}' not found in document '{doc.Name}'.")
        if body.TypeId != "PartDesign::Body":
            raise ValueError(f"'{name}' is not a PartDesign::Body (TypeId={body.TypeId}).")
        return body
    bodies = [o for o in doc.Objects if o.TypeId == "PartDesign::Body"]
    if len(bodies) == 1:
        return bodies[0]
    if not bodies:
        return doc.addObject("PartDesign::Body", "Body")
    raise ValueError(
        f"Document '{doc.Name}' has multiple Bodies "
        f"({[b.Name for b in bodies]}); pass 'body' in obj_properties."
    )


# --- geometry -----------------------------------------------------------------


def _vec2(p):
    if not isinstance(p, (list, tuple)) or len(p) != 2:
        raise ValueError(f"sketch coordinates must be [x, y], got {p!r}")
    return FreeCAD.Vector(float(p[0]), float(p[1]), 0.0)


def _build_geometry_item(item: dict):
    """spec geometry item -> Part geometry (sketch-local 2D, z=0)."""
    gtype = item.get("type")
    if gtype == "line":
        return Part.LineSegment(_vec2(item.get("from")), _vec2(item.get("to")))
    if gtype == "arc":
        center = _vec2(item.get("center"))
        radius = float(item.get("radius"))
        a0 = math.radians(float(item.get("start_angle")))
        a1 = math.radians(float(item.get("end_angle")))
        return Part.ArcOfCircle(Part.Circle(center, FreeCAD.Vector(0, 0, 1), radius), a0, a1)
    if gtype == "circle":
        return Part.Circle(
            _vec2(item.get("center")), FreeCAD.Vector(0, 0, 1), float(item.get("radius"))
        )
    if gtype == "bspline":
        points = item.get("points")
        if not isinstance(points, list) or len(points) < 2:
            raise ValueError("bspline requires a 'points' list with >= 2 points.")
        poles = [_vec2(p) for p in points]
        bc = Part.BSplineCurve()
        try:
            bc.buildFromPoles(poles, bool(item.get("periodic", False)))
        except TypeError:
            bc.buildFromPoles(poles)
            if item.get("periodic"):
                if hasattr(bc, "makePeriodic"):
                    bc.makePeriodic()
                else:
                    raise ValueError(
                        "periodic bspline not supported by this FreeCAD version."
                    ) from None
        return bc
    if gtype == "point":
        return Part.Point(_vec2(item.get("at")))
    raise ValueError(
        f"unknown geometry type {gtype!r}; supported: line, arc, circle, bspline, point"
    )


def _add_geometry(sketch, geometry: list, construction: list):
    construction_ids = {int(i) for i in (construction or [])}
    for expect_id, item in enumerate(geometry):
        if not isinstance(item, dict):
            raise ValueError(f"geometry[{expect_id}] must be a dict, got {item!r}")
        try:
            geo = _build_geometry_item(item)
        except (TypeError, KeyError) as e:
            raise ValueError(f"geometry[{expect_id}] ({item.get('type')}): {e}") from e
        geo_id = sketch.addGeometry(geo, expect_id in construction_ids)
        if geo_id != expect_id:
            raise RuntimeError(f"internal: expected GeoId {expect_id}, FreeCAD returned {geo_id}")


# --- external geometry ----------------------------------------------------------


def _external_binder(body, doc, ref, elem):
    """Bridge an out-of-body external target through a SubShapeBinder.

    PartDesign forbids external geometry outside the sketch's body; the
    canonical bridge is a SubShapeBinder inside the body referencing the
    target sub-element. Idempotent per (object, element) pair.
    """
    name = f"Ext_{ref.Name}_{elem}"
    binder = doc.getObject(name)
    if binder is None:
        binder = body.newObject("PartDesign::SubShapeBinder", name)
        binder.Support = [(ref, (elem,))]
        doc.recompute()
        view = getattr(binder, "ViewObject", None)
        if view is not None:
            view.Visibility = False
    return binder


def _add_external(sketch, doc, body, external):
    """spec external: [[obj_name, "EdgeN"|"VertexN"], ...] -> GeoIds -3, -4, ...

    External geometry lets a sketch reference another object's edges/vertices
    (cross-part parametric references). The i-th entry occupies GeoId -(3+i);
    constraints reference it like any geometry id, e.g. [-3, "start"].
    Targets outside the sketch's body are bridged via a SubShapeBinder
    (PartDesign scope rule); the binder's shape holds exactly that one
    sub-element, so it is referenced as its own Edge1/Vertex1.
    """
    for i, item in enumerate(external or []):
        if not (isinstance(item, (list, tuple)) and len(item) == 2):
            raise ValueError(
                f"external[{i}] must be [object_name, 'EdgeN'|'VertexN'], got {item!r}"
            )
        ref = doc.getObject(item[0])
        if ref is None:
            raise ValueError(f"external[{i}]: object '{item[0]}' not found.")
        elem = str(item[1])
        if elem.startswith("Edge"):
            pool, n = ref.Shape.Edges, int(elem[4:])
        elif elem.startswith("Vertex"):
            pool, n = ref.Shape.Vertexes, int(elem[6:])
        else:
            raise ValueError(f"external[{i}]: element must be Edge/Vertex, got {elem!r}")
        if n < 1 or n > len(pool):
            raise ValueError(
                f"external[{i}]: '{elem}' out of range on '{item[0]}' (1-{len(pool)})."
            )
        if ref in getattr(body, "Group", []):
            sketch.addExternal(ref.Name, elem)  # addExternal takes (str, str)
        else:
            binder = _external_binder(body, doc, ref, elem)
            # Binder shape = just this one sub-element -> its local name.
            sketch.addExternal(binder.Name, "Edge1" if elem.startswith("Edge") else "Vertex1")


def _check_external_point_refs(spec):
    """Reject 'mid'/'center' point refs on external GeoIds up front.

    External edges expose only start/end as constrainable points; a 'mid'
    reference passes addConstraint but fails at solve time with a cryptic
    MalformedConstraints diagnostic — validate early with an actionable error.
    """
    n_ext = len(spec.get("external") or [])
    if not n_ext:
        return
    ext_ids = {-(3 + i) for i in range(n_ext)}
    for ci, c in enumerate(spec.get("constraints") or []):
        for item in c.get("items") or []:
            if (
                isinstance(item, (list, tuple))
                and len(item) == 2
                and isinstance(item[0], int)
                and item[0] in ext_ids
                and str(item[1]).lower() in ("mid", "center")
            ):
                raise ValueError(
                    f"constraints[{ci}]: external geometry (GeoId {item[0]}) exposes "
                    "only 'start'/'end' points — 'mid'/'center' is not constrainable."
                )


# --- constraints ----------------------------------------------------------------


def _point_ref(item, what="item"):
    """[geo_id, "start"|"end"|...] -> (geo_id, PointPos).

    GeoId -1 is the sketch X axis; its start point (origin) is the common
    anchor, so any point keyword on -1 maps to the origin (PointPos 1).
    Bare [geo_id] (a whole-geometry reference) is handled by callers.
    """
    if not (isinstance(item, (list, tuple)) and len(item) == 2):
        raise ValueError(f"{what} must be [geo_id, point], got {item!r}")
    geo_id, pos = int(item[0]), str(item[1]).lower()
    if geo_id == -1:
        return -1, 1  # origin
    p = _POINT_POS.get(pos)
    if p is None:
        raise ValueError(f"{what}: point must be one of {sorted(_POINT_POS)}, got {item[1]!r}")
    return geo_id, p


def _geo_id(item, what="item"):
    if not (isinstance(item, (list, tuple)) and len(item) == 1):
        raise ValueError(f"{what} must be [geo_id], got {item!r}")
    return int(item[0])


def _is_point_item(item):
    return isinstance(item, (list, tuple)) and len(item) == 2


def _constraint_geometry(ctype: str, items: list):
    """spec constraint -> Sketcher.Constraint args (without any value)."""
    if ctype == "coincident":
        g1, p1 = _point_ref(items[0])
        g2, p2 = _point_ref(items[1])
        return ("Coincident", g1, p1, g2, p2)
    if ctype in ("horizontal", "vertical"):
        return (ctype.capitalize(), _geo_id(items[0]))
    if ctype in ("tangent", "perpendicular"):
        if all(_is_point_item(i) for i in items[:2]):
            g1, p1 = _point_ref(items[0])
            g2, p2 = _point_ref(items[1])
            return (ctype.capitalize(), g1, p1, g2, p2)
        return (ctype.capitalize(), _geo_id(items[0]), _geo_id(items[1]))
    if ctype in ("parallel", "equal"):
        return (ctype.capitalize(), _geo_id(items[0]), _geo_id(items[1]))
    if ctype == "symmetric":
        g1, p1 = _point_ref(items[0])
        g2, p2 = _point_ref(items[1])
        third = items[2]
        if _is_point_item(third):  # symmetry about a point
            g3, p3 = _point_ref(third)
            return ("Symmetric", g1, p1, g2, p2, g3, p3)
        return ("Symmetric", g1, p1, g2, p2, _geo_id(third))  # about a line
    if ctype == "distance":
        if len(items) == 1:
            return ("Distance", _geo_id(items[0]))
        a, b = items[0], items[1]
        if _is_point_item(a) and _is_point_item(b):
            g1, p1 = _point_ref(a)
            g2, p2 = _point_ref(b)
            return ("Distance", g1, p1, g2, p2)
        if _is_point_item(a):
            g1, p1 = _point_ref(a)
            return ("Distance", g1, p1, _geo_id(b))
        g2, p2 = _point_ref(b)
        return ("Distance", g2, p2, _geo_id(a))
    if ctype in ("distance_x", "distance_y"):
        fc_type = "DistanceX" if ctype == "distance_x" else "DistanceY"
        if len(items) == 1:
            g, p = _point_ref(items[0])
            return (fc_type, g, p)
        g1, p1 = _point_ref(items[0])
        g2, p2 = _point_ref(items[1])
        return (fc_type, g1, p1, g2, p2)
    if ctype == "radius":
        return ("Radius", _geo_id(items[0]))
    if ctype == "angle":
        if len(items) == 1:
            return ("Angle", _geo_id(items[0]))
        return ("Angle", _geo_id(items[0]), _geo_id(items[1]))
    raise ValueError(
        f"unknown constraint type {ctype!r}; supported: coincident, horizontal, "
        "vertical, tangent, perpendicular, parallel, equal, symmetric, distance, "
        "distance_x, distance_y, radius, angle"
    )


_DIMENSIONAL = ("distance", "distance_x", "distance_y", "radius", "angle")


def _add_constraints(sketch, constraints: list):
    for i, c in enumerate(constraints or []):
        if not isinstance(c, dict):
            raise ValueError(f"constraints[{i}] must be a dict, got {c!r}")
        ctype = c.get("type")
        items = c.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError(f"constraints[{i}] ({ctype}): 'items' must be a non-empty list.")
        try:
            args = _constraint_geometry(ctype, items)
        except (IndexError, TypeError) as e:
            raise ValueError(f"constraints[{i}] ({ctype}): malformed items: {e}") from e

        dimensional = ctype in _DIMENSIONAL
        value = c.get("value")
        if dimensional:
            if value is None:
                raise ValueError(
                    f"constraints[{i}] ({ctype}): dimensional constraint requires 'value'."
                )
            is_expr = isinstance(value, str) and value.startswith("=")
            if ctype == "angle" and not is_expr:
                datum = math.radians(float(value))
            else:
                datum = 1.0 if is_expr else float(value)  # placeholder for expr binding
            con = Sketcher.Constraint(*args, datum)
        else:
            if value is not None:
                raise ValueError(
                    f"constraints[{i}] ({ctype}): geometric constraint takes no 'value'."
                )
            con = Sketcher.Constraint(*args)

        idx = sketch.addConstraint(con)
        if c.get("name"):
            sketch.renameConstraint(idx, str(c["name"]))
        if dimensional and isinstance(value, str) and value.startswith("="):
            try:
                sketch.setExpression(f"Constraints[{idx}]", value[1:])
            except Exception as e:
                raise ValueError(
                    f"constraints[{i}] ({ctype}): expression binding failed: {e}"
                ) from e


# --- attachment -----------------------------------------------------------------


def _attach_sketch(sketch, doc, spec):
    plane = spec.get("plane", "XY")
    offset = float(spec.get("offset", 0) or 0)

    if isinstance(plane, str):
        key = plane.upper()
        if key not in _BASE_PLANES:
            raise ValueError(f"plane must be XY/XZ/YZ or {{'face': ...}}, got {plane!r}")
        _, axis, angle = _BASE_PLANES[key]
        normal = FreeCAD.Rotation(FreeCAD.Vector(*axis), angle).multVec(FreeCAD.Vector(0, 0, 1))
        sketch.Placement = FreeCAD.Placement(
            normal * offset, FreeCAD.Rotation(FreeCAD.Vector(*axis), angle)
        )
        return

    if isinstance(plane, dict) and plane.get("face"):
        face = plane["face"]
        if not (isinstance(face, (list, tuple)) and len(face) == 2):
            raise ValueError("plane.face must be [object_name, 'FaceN'].")
        ref = doc.getObject(face[0])
        if ref is None:
            raise ValueError(f"plane face object '{face[0]}' not found.")
        face_name = str(face[1])
        n = int(face_name[4:]) if face_name.startswith("Face") else 0
        if n < 1 or n > len(ref.Shape.Faces):
            raise ValueError(
                f"'{face_name}' out of range on '{ref.Name}' (1-{len(ref.Shape.Faces)})."
            )
        support_prop = "AttachmentSupport" if hasattr(sketch, "AttachmentSupport") else "Support"
        setattr(sketch, support_prop, [(ref, face_name)])
        sketch.MapMode = "FlatFace"
        if offset:
            sketch.AttachmentOffset = FreeCAD.Placement(
                FreeCAD.Vector(0, 0, offset), FreeCAD.Rotation()
            )
        return

    if isinstance(plane, dict) and plane.get("datum"):
        ref = doc.getObject(str(plane["datum"]))
        if ref is None:
            raise ValueError(f"datum plane '{plane['datum']}' not found.")
        support_prop = "AttachmentSupport" if hasattr(sketch, "AttachmentSupport") else "Support"
        setattr(sketch, support_prop, [(ref, "")])
        sketch.MapMode = "FlatFace"
        if offset:
            sketch.AttachmentOffset = FreeCAD.Placement(
                FreeCAD.Vector(0, 0, offset), FreeCAD.Rotation()
            )
        return

    raise ValueError(
        f"plane must be XY/XZ/YZ or {{'face': ...}} or {{'datum': ...}}, got {plane!r}"
    )


# --- solver diagnostics -----------------------------------------------------------


def _solver_diagnostics(sketch) -> dict:
    """Whatever solver diagnostics this FreeCAD version exposes (defensive)."""
    diag = {}
    for attr in (
        "ConflictingConstraints",
        "RedundantConstraints",
        "MalformedConstraints",
        "PartiallyRedundantConstraints",
    ):
        value = getattr(sketch, attr, None)
        if value:
            diag[attr] = [int(i) for i in value]
    return diag


# --- entry point ------------------------------------------------------------------


def build_sketch_gui(doc, spec):
    """Build a constrained sketch atomically; returns the SketchObject.

    Raises on any failure — the caller's transaction wrapper aborts, so the
    document stays untouched.
    """
    global _LAST_SKETCH_INFO
    _LAST_SKETCH_INFO = None

    geometry = spec.get("geometry")
    if not isinstance(geometry, list) or not geometry:
        raise ValueError("sketch requires a non-empty 'geometry' list.")

    body = _get_or_create_body(doc, spec.get("body"))
    sketch = body.newObject("Sketcher::SketchObject", spec.get("base") or "Sketch")

    _attach_sketch(sketch, doc, spec)
    _add_geometry(sketch, geometry, spec.get("construction"))
    _add_external(sketch, doc, body, spec.get("external"))
    _check_external_point_refs(spec)
    _add_constraints(sketch, spec.get("constraints"))
    doc.recompute()

    rc = sketch.solve()
    dof = getattr(sketch, "DoF", None)
    diag = _solver_diagnostics(sketch)
    if rc != 0 or (diag.get("ConflictingConstraints")):
        raise RuntimeError(
            f"sketch solve failed (rc={rc}); DoF={dof}; state={list(map(str, sketch.State))}; "
            f"diagnostics={diag}; constraints={len(spec.get('constraints') or [])}. "
            "Remove conflicting/redundant constraints and retry."
        )

    _LAST_SKETCH_INFO = {
        "object_name": sketch.Name,
        "body": body.Name,
        "dof": int(dof) if dof is not None else None,
        "fully_constrained": (dof == 0) if dof is not None else None,
        "geometry_count": len(geometry),
        "constraint_count": len(spec.get("constraints") or []),
        "external_count": len(spec.get("external") or []),
        "diagnostics": diag or None,
    }
    return sketch
