"""Anchor-based assembly: derive/store anchors, snap parts together, audit fit.

Coordinate rules (same as geometry_query): Shape accessors are GLOBAL.
Explicit anchors are stored in LOCAL coordinates (JSON in the ``MCP_Anchors``
string property) so they follow the object's Placement; they are converted
to global on every read.

All public functions return ``{"success": bool, ...}`` and never raise.
"""

import contextlib
import json
import math

import FreeCAD

from rpc_server.geometry_query import _face_normal, _get_obj, _r, _vec

ANCHOR_PROP = "MCP_Anchors"
_MAX_FLOATING_REPORT = 50
_MAX_INTERFERENCE_REPORT = 20


# ---------------------------------------------------------------------------
# anchor storage & resolution
# ---------------------------------------------------------------------------


def _load_explicit(obj):
    raw = getattr(obj, ANCHOR_PROP, None)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def _to_global(obj, entry):
    """Local-coords anchor entry -> (global_pos, global_dir|None)."""
    pos = obj.Placement.multVec(FreeCAD.Vector(*entry["pos"]))
    direction = None
    if entry.get("dir"):
        direction = obj.Placement.Rotation.multVec(FreeCAD.Vector(*entry["dir"]))
        if direction.Length > 1e-12:
            direction.normalize()
    return pos, direction


def _auto_anchor_map(obj):
    """Derive standard anchors from the Shape as RAW vectors (GLOBAL coords).

    Full precision — used for alignment/residual math. Use _auto_anchors for
    rounded report output."""
    shape = obj.Shape
    anchors = {}
    bb = shape.BoundBox
    anchors["bbox_center"] = (
        FreeCAD.Vector((bb.XMin + bb.XMax) / 2, (bb.YMin + bb.YMax) / 2, (bb.ZMin + bb.ZMax) / 2),
        None,
    )
    anchors["bbox_min"] = (FreeCAD.Vector(bb.XMin, bb.YMin, bb.ZMin), None)
    anchors["bbox_max"] = (FreeCAD.Vector(bb.XMax, bb.YMax, bb.ZMax), None)
    with contextlib.suppress(Exception):
        anchors["com"] = (FreeCAD.Vector(shape.CenterOfMass), None)

    # dominant cylindrical/conical face -> axis family
    cyl = [f for f in shape.Faces if "Cylinder" in f.Surface.TypeId or "Cone" in f.Surface.TypeId]
    if cyl:
        face = max(cyl, key=lambda f: f.Area or 0)
        axis = FreeCAD.Vector(face.Surface.Axis)
        if axis.Length > 1e-12:
            axis.normalize()
        center = face.CenterOfMass
        pts = [v.Point for v in face.Vertexes] or [center]
        projs = [(p - center).dot(axis) for p in pts]
        anchors["axis_mid"] = (FreeCAD.Vector(center), FreeCAD.Vector(axis))
        anchors["axis_start"] = (center + axis * min(projs), FreeCAD.Vector(axis))
        anchors["axis_end"] = (center + axis * max(projs), FreeCAD.Vector(axis))

    # up to 3 largest planar faces
    planar = sorted(
        (f for f in shape.Faces if "Plane" in f.Surface.TypeId), key=lambda f: -(f.Area or 0)
    )[:3]
    for i, face in enumerate(planar):
        anchors[f"face{i}_center"] = (FreeCAD.Vector(face.CenterOfMass), _face_normal(face))
    return anchors


def _auto_anchors(obj):
    """Rounded report form of _auto_anchor_map."""
    return {
        name: {"pos": _vec(pos), "dir": _vec(direction) if direction else None}
        for name, (pos, direction) in _auto_anchor_map(obj).items()
    }


def _resolve_anchor(obj, name):
    """-> (global_pos, global_dir|None, source, error). RAW precision vectors."""
    explicit = _load_explicit(obj)
    if name in explicit:
        try:
            pos, direction = _to_global(obj, explicit[name])
            return pos, direction, "explicit", None
        except Exception as e:
            return None, None, None, f"anchor '{name}': bad stored data ({e})"
    auto = _auto_anchor_map(obj)
    if name in auto:
        pos, direction = auto[name]
        return FreeCAD.Vector(pos), direction, "auto", None
    known = sorted(set(explicit) | set(auto))
    return (
        None,
        None,
        None,
        (f"anchor '{name}' not found on '{obj.Name}'. Available: {', '.join(known)}"),
    )


def get_anchors(doc_name, obj_name):
    obj, err = _get_obj(doc_name, obj_name)
    if err:
        return {"success": False, "error": err}
    shape = getattr(obj, "Shape", None)
    if shape is None or shape.isNull():
        return {"success": False, "error": f"Object '{obj_name}' has no Shape."}
    try:
        anchors = {}
        for name, entry in _auto_anchors(obj).items():
            anchors[name] = {**entry, "source": "auto"}
        for name, entry in _load_explicit(obj).items():
            pos, direction = _to_global(obj, entry)
            anchors[name] = {
                "pos": _vec(pos),
                "dir": _vec(direction) if direction else None,
                "source": "explicit",
            }
        return {"success": True, "obj_name": obj.Name, "anchors": anchors}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


def set_anchors(doc_name, obj_name, anchors, replace=False, coord_frame="local"):
    """Store explicit anchors. coord_frame: "local" (stored as-is) or "global"
    (document coordinates, converted to local via the inverse Placement so the
    anchor still follows future Placement moves)."""
    obj, err = _get_obj(doc_name, obj_name)
    if err:
        return {"success": False, "error": err}
    if coord_frame not in ("local", "global"):
        return {
            "success": False,
            "error": f"coord_frame must be 'local' or 'global', got {coord_frame!r}",
        }
    try:
        inv = obj.Placement.inverse() if coord_frame == "global" else None
        inv_rot = inv.Rotation if inv is not None else None
        cleaned = {}
        for name, entry in anchors.items():
            name = str(name).strip()
            if not name:
                return {"success": False, "error": "anchor names must be non-empty"}
            pos = entry.get("pos") if isinstance(entry, dict) else None
            if not (isinstance(pos, (list, tuple)) and len(pos) == 3):
                return {"success": False, "error": f"anchor '{name}': pos must be [x, y, z]"}
            direction = entry.get("dir")
            if direction is not None and not (
                isinstance(direction, (list, tuple)) and len(direction) == 3
            ):
                return {
                    "success": False,
                    "error": f"anchor '{name}': dir must be [x, y, z] or null",
                }
            if inv is not None:
                pos_v = inv.multVec(FreeCAD.Vector(*pos))
                pos = [pos_v.x, pos_v.y, pos_v.z]
                if direction is not None:
                    dir_v = inv_rot.multVec(FreeCAD.Vector(*direction))
                    direction = [dir_v.x, dir_v.y, dir_v.z]
            cleaned[name] = {
                "pos": [float(v) for v in pos],
                "dir": [float(v) for v in direction] if direction else None,
            }
        if ANCHOR_PROP not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyString",
                ANCHOR_PROP,
                "MCP",
                "Named assembly anchors (JSON, local coords)",
            )
        merged = {} if replace else _load_explicit(obj)
        merged.update(cleaned)
        setattr(obj, ANCHOR_PROP, json.dumps(merged, separators=(",", ":")))
        return {"success": True, "obj_name": obj.Name, "anchor_count": len(cleaned)}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# assemble — snap anchors together, measure residuals
# ---------------------------------------------------------------------------


def _rotation_from_to(from_vec, to_vec):
    """Shortest-arc Rotation taking from_vec to to_vec (both normalized)."""
    if from_vec.isEqual(to_vec, 1e-9):
        return FreeCAD.Rotation()
    cross = from_vec.cross(to_vec)
    if cross.Length < 1e-10:
        perp = from_vec.cross(FreeCAD.Vector(1, 0, 0))
        if perp.Length < 1e-6:
            perp = from_vec.cross(FreeCAD.Vector(0, 1, 0))
        perp.normalize()
        return FreeCAD.Rotation(perp, 180.0)
    angle = math.degrees(from_vec.getAngle(to_vec))
    return FreeCAD.Rotation(cross.normalize(), angle)


def _apply_mate(obj, a_pos, a_dir, t_pos, t_dir, mode, offset):
    """Compute the new Placement for one mate. -> (new_placement, warning|None)"""
    placement = obj.Placement
    new_base = FreeCAD.Vector(placement.Base)
    new_rotation = FreeCAD.Rotation(placement.Rotation)
    local_pos = placement.inverse().multVec(a_pos)
    warning = None

    if mode != "center" and (a_dir is None or t_dir is None):
        warning = f"mode '{mode}' fell back to 'center' (anchor without dir)"
        mode = "center"

    if mode == "center":
        desired_pos = t_pos
    else:
        t_dir_n = FreeCAD.Vector(t_dir)
        t_dir_n.normalize()
        # touch: directions oppose (face-to-face); axis: directions parallel
        desired_dir = (
            FreeCAD.Vector(-t_dir_n.x, -t_dir_n.y, -t_dir_n.z) if mode == "touch" else t_dir_n
        )
        a_dir_n = FreeCAD.Vector(a_dir)
        a_dir_n.normalize()
        delta_rot = _rotation_from_to(a_dir_n, desired_dir)
        new_rotation = delta_rot.multiply(new_rotation)
        desired_pos = t_pos + t_dir_n * float(offset)

    rotated_pos = new_rotation.multVec(local_pos) + new_base
    new_base = new_base + (desired_pos - rotated_pos)
    return FreeCAD.Placement(new_base, new_rotation), warning


def assemble(doc_name, mates, tolerance=0.1, stop_on_error=True):
    results = []
    passed = failed = 0
    try:
        for i, mate in enumerate(mates):
            label = f"mate[{i}] {mate.get('obj')}.{mate.get('anchor')}"
            entry = {
                "index": i,
                "obj": mate.get("obj"),
                "anchor": mate.get("anchor"),
                "target": mate.get("target"),
                "target_anchor": mate.get("target_anchor"),
                "mode": mate.get("mode", "center"),
            }
            obj, err = _get_obj(doc_name, str(mate.get("obj", "")))
            target, terr = (None, None)
            if not err:
                target, terr = _get_obj(doc_name, str(mate.get("target", "")))
            if err or terr:
                entry.update(passed=False, error=f"{label}: {err or terr}")
            else:
                a_pos, a_dir, _, aerr = _resolve_anchor(obj, str(mate.get("anchor", "")))
                t_pos, t_dir, _, t_err = _resolve_anchor(target, str(mate.get("target_anchor", "")))
                if aerr or t_err:
                    entry.update(passed=False, error=aerr or f"target: {t_err}")
                else:
                    new_pl, warn = _apply_mate(
                        obj, a_pos, a_dir, t_pos, t_dir, entry["mode"], mate.get("offset", 0.0)
                    )
                    if warn:
                        entry["warning"] = warn
                    obj.Placement = new_pl
                    # re-resolve AFTER the move to measure the true residual
                    n_pos, n_dir, _, _ = _resolve_anchor(obj, str(mate.get("anchor", "")))
                    t_off = FreeCAD.Vector(0, 0, 0)
                    if t_dir is not None and entry["mode"] != "center":
                        d = FreeCAD.Vector(t_dir)
                        d.normalize()
                        t_off = d * float(mate.get("offset", 0.0))
                    residual = (n_pos - (t_pos + t_off)).Length
                    entry["residual_mm"] = _r(residual)
                    if n_dir is not None and t_dir is not None and entry["mode"] != "center":
                        want = FreeCAD.Vector(t_dir)
                        if entry["mode"] == "touch":
                            want = FreeCAD.Vector(-want.x, -want.y, -want.z)
                        entry["residual_deg"] = _r(math.degrees(n_dir.getAngle(want)))
                    if residual <= float(tolerance):
                        entry["passed"] = True
                    else:
                        entry.update(
                            passed=False,
                            error=f"residual {_r(residual)}mm exceeds "
                            f"tolerance {_r(float(tolerance))}mm",
                        )
            if entry["passed"]:
                passed += 1
            else:
                failed += 1
                if stop_on_error:
                    results.append(entry)
                    return {
                        "success": False,
                        "passed": passed,
                        "failed": failed,
                        "mates": results,
                        "error": f"aborted at {label}: {entry.get('error')}",
                    }
            results.append(entry)
        return {
            "success": failed == 0,
            "passed": passed,
            "failed": failed,
            "mates": results,
            "error": None if failed == 0 else f"{failed} mate(s) failed",
        }
    except Exception as e:
        return {
            "success": False,
            "passed": passed,
            "failed": failed,
            "mates": results,
            "error": f"{type(e).__name__}: {e}",
        }


# ---------------------------------------------------------------------------
# verify_assembly — numeric spatial audit
# ---------------------------------------------------------------------------


def _bbox_dist(b1, b2):
    """Lower-bound distance between two BoundBoxes (0 when overlapping)."""
    dx = max(b1.XMin - b2.XMax, b2.XMin - b1.XMax, 0.0)
    dy = max(b1.YMin - b2.YMax, b2.YMin - b1.YMax, 0.0)
    dz = max(b1.ZMin - b2.ZMax, b2.ZMin - b1.ZMax, 0.0)
    return math.sqrt(dx * dx + dy * dy + dz * dz)


_CONTACT_TOLERANCE = 0.5  # mm — exact distance that counts as touching
_MAX_ISLAND_REPORT = 10
_MAX_ISLAND_OBJECTS = 20


class _UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def verify_assembly(doc_name, checks=None, float_threshold=1.0, interference_min_volume=1.0):
    try:
        doc = FreeCAD.getDocument(doc_name)
    except Exception:
        return {"success": False, "error": f"Document '{doc_name}' not found."}
    try:
        shaped = [
            (o.Name, o.Shape)
            for o in doc.Objects
            if getattr(o, "Shape", None) is not None and not o.Shape.isNull()
        ]
        float_threshold = float(float_threshold)
        interference_min_volume = float(interference_min_volume)

        # nearest-neighbour distance per object (bbox lower bound first,
        # exact distToShape only for bbox-near pairs). The scan range covers
        # the contact tolerance so contact edges are collected for free.
        scan_range = max(float_threshold, _CONTACT_TOLERANCE)
        name_index = {name: i for i, (name, _) in enumerate(shaped)}
        edges = []  # contact edges as index pairs
        floating = []
        for i, (name, shape) in enumerate(shaped):
            best_exact = None
            best_bbox = math.inf
            best_name = None
            near = []
            for j, (other_name, other) in enumerate(shaped):
                if i == j:
                    continue
                d = _bbox_dist(shape.BoundBox, other.BoundBox)
                if d < best_bbox:
                    best_bbox, best_name = d, other_name
                if d <= scan_range:
                    near.append((other_name, other))
            if near:
                dists = [(on, shape.distToShape(o)[0]) for on, o in near]
                best_name, best_exact = min(dists, key=lambda t: t[1])
                for on, d in dists:
                    if d <= _CONTACT_TOLERANCE:
                        edges.append((i, name_index[on]))
            nearest = best_exact if best_exact is not None else best_bbox
            if nearest > float_threshold:
                floating.append({"obj": name, "nearest": best_name, "distance_mm": _r(nearest)})
        floating.sort(key=lambda f: -(f["distance_mm"] or 0))

        # interference: exact common volume for bbox-overlapping pairs only
        interferences = []
        for i in range(len(shaped)):
            name_a, shape_a = shaped[i]
            for j in range(i + 1, len(shaped)):
                name_b, shape_b = shaped[j]
                if _bbox_dist(shape_a.BoundBox, shape_b.BoundBox) > 0:
                    continue
                vol = shape_a.common(shape_b).Volume
                if vol > interference_min_volume:
                    interferences.append({"a": name_a, "b": name_b, "common_volume_mm3": _r(vol)})
                    edges.append((i, j))
        interferences.sort(key=lambda x: -(x["common_volume_mm3"] or 0))

        # contact graph: connected components over touch/interference edges.
        # The largest component is the main assembly; the rest are islands.
        uf = _UnionFind(len(shaped))
        for a, b in edges:
            uf.union(a, b)
        groups = {}
        for i, (name, _) in enumerate(shaped):
            groups.setdefault(uf.find(i), []).append(name)
        components = sorted(groups.values(), key=len, reverse=True)
        main = set(components[0]) if components else set()
        main_shapes = [(n, s) for n, s in shaped if n in main]
        islands = []
        for comp in components[1:]:
            # gap to main: pick the island/main pair with the smallest bbox
            # distance (cheap), then measure that pair exactly (one OCCT call
            # per island).
            best_pair, best_bbox = None, math.inf
            for name in comp:
                shape = shaped[name_index[name]][1]
                for mn, mshape in main_shapes:
                    d = _bbox_dist(shape.BoundBox, mshape.BoundBox)
                    if d < best_bbox:
                        best_bbox, best_pair = d, (mn, shape, mshape)
            best_gap, best_main = None, None
            if best_pair is not None:
                best_main, s1, s2 = best_pair
                best_gap = s1.distToShape(s2)[0]
            islands.append(
                {
                    "objects": sorted(comp)[:_MAX_ISLAND_OBJECTS],
                    "size": len(comp),
                    "gap_mm": _r(best_gap) if best_gap is not None else None,
                    "nearest_main": best_main,
                }
            )
        islands.sort(key=lambda x: -(x["gap_mm"] or 0))

        # explicit anchor-pair checks
        check_results = []
        for c in checks or []:
            obj, err = _get_obj(doc_name, str(c.get("obj", "")))
            target, terr = (None, None)
            if not err:
                target, terr = _get_obj(doc_name, str(c.get("target", "")))
            if err or terr:
                check_results.append({**c, "passed": False, "error": err or terr})
                continue
            a_pos, _, _, aerr = _resolve_anchor(obj, str(c.get("anchor", "")))
            t_pos, _, _, t_err = _resolve_anchor(target, str(c.get("target_anchor", "")))
            if aerr or t_err:
                check_results.append({**c, "passed": False, "error": aerr or t_err})
                continue
            dist = (a_pos - t_pos).Length
            tol = float(c.get("tolerance", 0.1))
            check_results.append(
                {
                    "obj": c["obj"],
                    "anchor": c["anchor"],
                    "target": c["target"],
                    "target_anchor": c["target_anchor"],
                    "distance_mm": _r(dist),
                    "tolerance": _r(tol),
                    "passed": dist <= tol,
                }
            )

        return {
            "success": True,
            "object_count": len(shaped),
            "floating": floating[:_MAX_FLOATING_REPORT],
            "interferences": interferences[:_MAX_INTERFERENCE_REPORT],
            "islands": islands[:_MAX_ISLAND_REPORT],
            "checks": check_results,
            "summary": {
                "floating_count": len(floating),
                "interference_count": len(interferences),
                "island_count": len(islands),
                "component_count": len(components),
                "checks_failed": sum(1 for c in check_results if not c.get("passed")),
            },
        }
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}
