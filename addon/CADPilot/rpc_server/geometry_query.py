"""Read-only geometric queries on object Shapes (measure / topology / interference).

All functions run on the FreeCAD GUI thread (dispatched by rpc_server) and
never mutate the document — no transaction, no recompute.

Coordinate convention: FreeCAD Shapes carry the object's Placement as their
internal location, so ``BoundBox``, ``CenterOfMass``, ``Vertex.Point``,
``Face.Surface`` (Axis/Center/Radius), ``Face.normalAt`` and ``Edge.Curve``
already return values in GLOBAL (document) coordinates. Do NOT apply
``obj.Placement`` again — that double-transforms everything.
"""

import math

import FreeCAD


def _get_obj(doc_name, obj_name):
    """Return (obj, None) or (None, error_message)."""
    try:
        doc = FreeCAD.getDocument(doc_name)
    except Exception:
        return None, f"Document '{doc_name}' not found."
    obj = doc.getObject(obj_name)
    if obj is None:
        return None, f"Object '{obj_name}' not found in document '{doc_name}'."
    return obj, None


def _get_shape(doc_name, obj_name):
    """Return (shape, None) or (None, error_message)."""
    obj, err = _get_obj(doc_name, obj_name)
    if err:
        return None, err
    shape = getattr(obj, "Shape", None)
    if shape is None or shape.isNull():
        return None, f"Object '{obj_name}' has no Shape."
    return shape, None


def _r(value):
    """Round to 4 significant digits to keep RPC payloads small."""
    try:
        return float(f"{float(value):.4g}")
    except (TypeError, ValueError):
        return None


def _vec(v):
    return [_r(v.x), _r(v.y), _r(v.z)]


def _short_type(type_id: str) -> str:
    """'Part::GeomPlane' -> 'Plane'; falls back to the raw string."""
    short = type_id.split("::")[-1]
    return short.removeprefix("Geom")


def _placement_info(pl):
    return {
        "base": _vec(pl.Base),
        "rotation": {
            "axis": _vec(pl.Rotation.Axis),
            "angle_deg": _r(math.degrees(pl.Rotation.Angle)),
            "yaw_pitch_roll": [_r(a) for a in pl.Rotation.getYawPitchRoll()],
        },
    }


# ---------------------------------------------------------------------------
# measure_geometry — GLOBAL coordinates
# ---------------------------------------------------------------------------


def measure_geometry(doc_name, obj_name):
    obj, err = _get_obj(doc_name, obj_name)
    if err:
        return {"success": False, "error": err}
    try:
        # obj.Shape is already in global coordinates (see module docstring).
        gshape = obj.Shape
        if gshape is None or gshape.isNull():
            return {"success": False, "error": f"Object '{obj_name}' has no Shape."}

        bb = gshape.BoundBox
        solids = gshape.Solids
        if gshape.ShapeType == "Compound" and solids:
            total_vol = sum(s.Volume for s in solids)
            if total_vol > 0:
                com = FreeCAD.Vector(
                    sum(s.CenterOfMass.x * s.Volume for s in solids) / total_vol,
                    sum(s.CenterOfMass.y * s.Volume for s in solids) / total_vol,
                    sum(s.CenterOfMass.z * s.Volume for s in solids) / total_vol,
                )
            else:
                com = solids[0].CenterOfMass
        else:
            com = gshape.CenterOfMass

        return {
            "success": True,
            "shape_type": gshape.ShapeType,
            "is_valid": bool(gshape.isValid()),
            "volume_mm3": _r(gshape.Volume),
            "area_mm2": _r(gshape.Area),
            "center_of_mass": _vec(com),
            "bbox": {
                "xmin": _r(bb.XMin),
                "ymin": _r(bb.YMin),
                "zmin": _r(bb.ZMin),
                "xmax": _r(bb.XMax),
                "ymax": _r(bb.YMax),
                "zmax": _r(bb.ZMax),
            },
            "counts": {
                "solids": len(gshape.Solids),
                "shells": len(gshape.Shells),
                "faces": len(gshape.Faces),
                "edges": len(gshape.Edges),
                "vertices": len(gshape.Vertexes),
            },
            "placement": _placement_info(obj.Placement),
        }
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# get_topology — edge endpoints, face radius/axis, all GLOBAL coords
# ---------------------------------------------------------------------------


def _face_normal(face):
    """Normal at the face centre (global coordinates)."""
    try:
        umin, umax, vmin, vmax = face.ParameterRange
        return face.normalAt((umin + umax) / 2, (vmin + vmax) / 2)
    except Exception:
        return None


def _face_entry(index, face):
    entry = {
        "index": index,
        "name": f"Face{index + 1}",
        "type": _short_type(face.Surface.TypeId),
        "area": _r(face.Area),
        "center": _vec(face.CenterOfMass),
    }
    normal = _face_normal(face)
    if normal is not None:
        entry["normal"] = _vec(normal)

    # Add radius and axis for cylindrical/conical/spherical surfaces
    surf = face.Surface
    type_id = surf.TypeId
    if "Cylinder" in type_id:
        try:
            entry["radius"] = _r(surf.Radius)
            entry["axis"] = _vec(surf.Axis)
        except Exception:
            pass
    elif "Cone" in type_id:
        try:
            entry["radius"] = _r(surf.Radius)  # radius at reference
            entry["axis"] = _vec(surf.Axis)
            entry["half_angle"] = _r(surf.SemiAngle)
        except Exception:
            pass
    elif "Sphere" in type_id:
        try:
            entry["radius"] = _r(surf.Radius)
            entry["center"] = _vec(surf.Center)
        except Exception:
            pass

    return entry


def _edge_entry(index, edge):
    entry = {
        "index": index,
        "name": f"Edge{index + 1}",
        "type": _short_type(edge.Curve.TypeId),
        "length": _r(edge.Length),
        "center": _vec(edge.CenterOfMass),
    }

    # Add start/end vertices (global). Closed edges (full circles) have a
    # single vertex — report end == start so the schema stays consistent.
    try:
        verts = edge.Vertexes
        if len(verts) >= 1:
            entry["start"] = _vec(verts[0].Point)
            entry["end"] = _vec(verts[-1].Point)
    except Exception:
        pass

    # Add radius/center/axis for circular edges
    curve = edge.Curve
    type_id = curve.TypeId
    if "Circle" in type_id or "ArcOfCircle" in type_id:
        try:
            entry["radius"] = _r(curve.Radius)
            entry["axis"] = _vec(curve.Axis)
            entry["center"] = _vec(curve.Center)  # circle centre beats COM
        except Exception:
            pass

    return entry


def _vertex_entry(index, vertex):
    pos = vertex.Point
    return {
        "index": index,
        "name": f"Vertex{index + 1}",
        "type": "Vertex",
        "x": _r(pos.x),
        "y": _r(pos.y),
        "z": _r(pos.z),
    }


def get_topology(doc_name, obj_name, element="faces", limit=50, offset=0):
    obj, err = _get_obj(doc_name, obj_name)
    if err:
        return {"success": False, "error": err}
    shape = getattr(obj, "Shape", None)
    if shape is None or shape.isNull():
        return {"success": False, "error": f"Object '{obj_name}' has no Shape."}
    if element not in ("faces", "edges", "vertices"):
        return {
            "success": False,
            "error": f"element must be 'faces', 'edges', or 'vertices', got {element!r}",
        }
    try:
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
        if element == "faces":
            entries = [_face_entry(i, f) for i, f in enumerate(shape.Faces)]
            entries.sort(key=lambda e: (-(e["area"] or 0), e["index"]))
            key = "faces"
        elif element == "edges":
            entries = [_edge_entry(i, e) for i, e in enumerate(shape.Edges)]
            entries.sort(key=lambda e: (-(e["length"] or 0), e["index"]))
            key = "edges"
        else:  # vertices
            entries = [_vertex_entry(i, v) for i, v in enumerate(shape.Vertexes)]
            entries.sort(key=lambda e: (-(e["x"] ** 2 + e["y"] ** 2 + e["z"] ** 2), e["index"]))
            key = "vertices"
        return {
            "success": True,
            "element": element,
            "total": len(entries),
            "returned": len(entries[offset : offset + limit]),
            "offset": offset,
            key: entries[offset : offset + limit],
        }
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# check_interference
# ---------------------------------------------------------------------------


def check_interference(doc_name, obj_a, obj_b):
    shape_a, err = _get_shape(doc_name, obj_a)
    if err:
        return {"success": False, "error": err}
    shape_b, err = _get_shape(doc_name, obj_b)
    if err:
        return {"success": False, "error": err}
    try:
        distance = shape_a.distToShape(shape_b)[0]
        common_volume = shape_a.common(shape_b).Volume
        return {
            "success": True,
            "distance_mm": _r(distance),
            "intersects": bool(common_volume > 1e-4),
            "common_volume_mm3": _r(common_volume),
        }
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# get_positioning_info — full spatial data for a specific element (GLOBAL coords)
# ---------------------------------------------------------------------------


def get_positioning_info(doc_name, obj_name, element, element_index):
    """Return detailed global-coordinate spatial info for a specific face/edge/vertex.

    element: "face" | "edge" | "vertex"
    element_index: 0-based index
    """
    obj, err = _get_obj(doc_name, obj_name)
    if err:
        return {"success": False, "error": err}
    shape = getattr(obj, "Shape", None)
    if shape is None or shape.isNull():
        return {"success": False, "error": f"Object '{obj_name}' has no Shape."}

    try:
        idx = int(element_index)
        if element == "face":
            faces = shape.Faces
            if idx < 0 or idx >= len(faces):
                return {
                    "success": False,
                    "error": f"Face index {idx} out of range (0..{len(faces) - 1})",
                }
            face = faces[idx]
            surf = face.Surface
            result = {
                "success": True,
                "element": "face",
                "name": f"Face{idx + 1}",
                "type": _short_type(surf.TypeId),
                "area": _r(face.Area),
                "center": _vec(face.CenterOfMass),
                "placement": _placement_info(obj.Placement),
            }
            normal = _face_normal(face)
            if normal is not None:
                result["normal"] = _vec(normal)

            type_id = surf.TypeId
            if "Cylinder" in type_id:
                result["radius"] = _r(surf.Radius)
                result["axis"] = _vec(surf.Axis)
            elif "Cone" in type_id:
                result["radius"] = _r(surf.Radius)
                result["axis"] = _vec(surf.Axis)
                result["half_angle"] = _r(surf.SemiAngle)
            elif "Sphere" in type_id:
                result["radius"] = _r(surf.Radius)
                result["center"] = _vec(surf.Center)
            elif "Plane" in type_id:
                result["position"] = _vec(surf.Position)

            return result

        elif element == "edge":
            edges = shape.Edges
            if idx < 0 or idx >= len(edges):
                return {
                    "success": False,
                    "error": f"Edge index {idx} out of range (0..{len(edges) - 1})",
                }
            edge = edges[idx]
            curve = edge.Curve
            result = {
                "success": True,
                "element": "edge",
                "name": f"Edge{idx + 1}",
                "type": _short_type(curve.TypeId),
                "length": _r(edge.Length),
                "center": _vec(edge.CenterOfMass),
                "placement": _placement_info(obj.Placement),
            }
            verts = edge.Vertexes
            if len(verts) >= 1:
                result["start"] = _vec(verts[0].Point)
            if len(verts) >= 2:
                result["end"] = _vec(verts[-1].Point)

            type_id = curve.TypeId
            if "Circle" in type_id or "ArcOfCircle" in type_id:
                result["radius"] = _r(curve.Radius)
                result["axis"] = _vec(curve.Axis)
                result["center"] = _vec(curve.Center)
            elif "Line" in type_id:
                result["direction"] = _vec(curve.Direction)

            return result

        elif element == "vertex":
            verts = shape.Vertexes
            if idx < 0 or idx >= len(verts):
                return {
                    "success": False,
                    "error": f"Vertex index {idx} out of range (0..{len(verts) - 1})",
                }
            vert = verts[idx]
            return {
                "success": True,
                "element": "vertex",
                "name": f"Vertex{idx + 1}",
                "position": _vec(vert.Point),
                "placement": _placement_info(obj.Placement),
            }
        else:
            return {
                "success": False,
                "error": f"element must be 'face', 'edge', or 'vertex', got {element!r}",
            }

    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# align_shapes — compute Placement to align one object's element to another's
# ---------------------------------------------------------------------------


def _element_spatial(obj, element, idx):
    """Return (global_center, global_normal, global_axis, local_center) for an element.

    Shape accessors are global; local_center is mapped back through the
    inverse Placement for new-placement composition math.
    """
    shape = obj.Shape
    placement = obj.Placement
    inv = placement.inverse()

    if element == "face":
        if idx < 0 or idx >= len(shape.Faces):
            return None, f"Face index {idx} out of range"
        face = shape.Faces[idx]
        global_center = face.CenterOfMass
        global_normal = _face_normal(face)
        global_axis = None
        if "Cylinder" in face.Surface.TypeId or "Cone" in face.Surface.TypeId:
            global_axis = face.Surface.Axis
    elif element == "edge":
        if idx < 0 or idx >= len(shape.Edges):
            return None, f"Edge index {idx} out of range"
        edge = shape.Edges[idx]
        global_center = edge.CenterOfMass
        global_normal = None
        global_axis = None
        if "Circle" in edge.Curve.TypeId:
            global_axis = edge.Curve.Axis
    elif element == "vertex":
        if idx < 0 or idx >= len(shape.Vertexes):
            return None, f"Vertex index {idx} out of range"
        global_center = shape.Vertexes[idx].Point
        global_normal = None
        global_axis = None
    else:
        return None, "element must be 'face', 'edge', or 'vertex'"

    local_center = inv.multVec(global_center)
    return (global_center, global_normal, global_axis, local_center), None


def align_shapes(
    doc_name,
    obj_name,
    element,
    element_index,
    target_obj_name,
    target_element,
    target_element_index,
    mode="touch",
    offset=0.0,
):
    """Compute and apply a new Placement to obj_name so its element aligns with
    target_obj_name's element.

    mode:
      "touch"  — face: bring faces together with normals opposing; edge/vertex: center-to-center
      "center" — align element centers (translation only, no rotation change)
      "axis"   — align cylindrical face axes (rotation + translation)

    offset: extra distance along the target normal direction after alignment
            (touch mode only; ignored with a warning in center/axis modes).
    """
    obj, err = _get_obj(doc_name, obj_name)
    if err:
        return {"success": False, "error": err}
    target_obj, err = _get_obj(doc_name, target_obj_name)
    if err:
        return {"success": False, "error": err}

    shape = getattr(obj, "Shape", None)
    target_shape = getattr(target_obj, "Shape", None)
    if shape is None or shape.isNull():
        return {"success": False, "error": f"Object '{obj_name}' has no Shape."}
    if target_shape is None or target_shape.isNull():
        return {"success": False, "error": f"Object '{target_obj_name}' has no Shape."}

    try:
        info, err = _element_spatial(obj, element, int(element_index))
        if err:
            return {"success": False, "error": err}
        global_center, global_normal, global_axis, local_center = info

        t_info, err = _element_spatial(target_obj, target_element, int(target_element_index))
        if err:
            return {"success": False, "error": f"Target: {err}"}
        t_global_center, t_global_normal, t_global_axis, _ = t_info

        placement = obj.Placement
        rot = placement.Rotation

        new_base = FreeCAD.Vector(placement.Base)
        new_rotation = FreeCAD.Rotation(placement.Rotation)
        ignored_offset = None

        if mode == "center":
            # Simple: translate so element center matches target element center
            delta = t_global_center - global_center
            new_base = new_base + delta
            if float(offset) != 0.0:
                ignored_offset = "offset is only applied in 'touch' mode; it was ignored here."

        elif mode == "touch" and element == "face" and target_element == "face":
            # Face-to-face touch: rotate so the obj face normal opposes the
            # target face normal, then translate so the face centre sits on
            # the target face plane (plus offset along the target normal).
            desired_normal = FreeCAD.Vector(
                -t_global_normal.x, -t_global_normal.y, -t_global_normal.z
            )

            if not global_normal.isEqual(desired_normal, 1e-6):
                cross = global_normal.cross(desired_normal)
                if cross.Length > 1e-10:
                    angle = global_normal.getAngle(desired_normal)
                    rot_axis = cross.normalize()
                    delta_rot = FreeCAD.Rotation(rot_axis, math.degrees(float(angle)))
                    new_rotation = delta_rot.multiply(rot)
                else:
                    # Normals are parallel but opposite — 180° around any perpendicular axis
                    perp = global_normal.cross(FreeCAD.Vector(1, 0, 0))
                    if perp.Length < 1e-6:
                        perp = global_normal.cross(FreeCAD.Vector(0, 1, 0))
                    perp.normalize()
                    delta_rot = FreeCAD.Rotation(perp, 180.0)
                    new_rotation = delta_rot.multiply(rot)

            # Where the face centre ends up after rotation
            new_global_center = new_rotation.multVec(local_center) + new_base
            target_pos = t_global_center + FreeCAD.Vector(
                t_global_normal.x, t_global_normal.y, t_global_normal.z
            ) * float(offset)
            translation = target_pos - new_global_center
            new_base = new_base + translation

        elif mode == "axis" and global_axis is not None and t_global_axis is not None:
            # Align cylindrical axes: rotate obj so its axis matches the target
            # axis, then translate so the element centre lies on the target axis line.
            neg_t = FreeCAD.Vector(-t_global_axis.x, -t_global_axis.y, -t_global_axis.z)
            if not global_axis.isEqual(t_global_axis, 1e-6) and not global_axis.isEqual(
                neg_t, 1e-6
            ):
                cross = global_axis.cross(t_global_axis)
                if cross.Length > 1e-10:
                    angle = global_axis.getAngle(t_global_axis)
                    rot_axis = cross.normalize()
                    delta_rot = FreeCAD.Rotation(rot_axis, math.degrees(float(angle)))
                    new_rotation = delta_rot.multiply(rot)

            new_global_center = new_rotation.multVec(local_center) + new_base
            axis_origin = t_global_center
            axis_dir = FreeCAD.Vector(t_global_axis.x, t_global_axis.y, t_global_axis.z)
            axis_dir.normalize()
            diff = new_global_center - axis_origin
            proj_len = diff.dot(axis_dir)
            closest_on_axis = axis_origin + axis_dir * proj_len
            translation = closest_on_axis - new_global_center
            new_base = new_base + translation
            if float(offset) != 0.0:
                ignored_offset = "offset is only applied in 'touch' mode; it was ignored here."

        else:
            return {
                "success": False,
                "error": f"mode '{mode}' not supported for element types '{element}'->'{target_element}'. "
                f"Supported: center (any), touch (face->face), axis (cylindrical face->cylindrical face)",
            }

        # Apply the new Placement
        new_placement = FreeCAD.Placement(new_base, new_rotation)
        obj.Placement = new_placement

        result = {
            "success": True,
            "obj_name": obj_name,
            "new_placement": {
                "base": _vec(new_base),
                "rotation": {
                    "axis": _vec(new_rotation.Axis),
                    "angle_deg": _r(math.degrees(new_rotation.Angle)),
                },
            },
        }
        if ignored_offset:
            result["warning"] = ignored_offset
        return result

    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}
