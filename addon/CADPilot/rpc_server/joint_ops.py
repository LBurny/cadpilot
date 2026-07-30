"""Persistent-joint assembly: FreeCAD 1.1 Assembly workbench lifecycle.

Recipe (verified live on FreeCAD 1.1.3):
  asm = doc.addObject("Assembly::AssemblyObject", name); asm.Type = "Assembly"
  component = App::Link wrapping the part; the LINK owns the Placement
  (link.Placement = part.Placement; part.Placement = identity; part hidden)
  joints live in UtilsAssembly.getJointGroup(asm) as App::FeaturePython with
  JointObject.Joint proxy; references are (link, ["FaceN", "VertexM"]) — the
  vertex decides WHERE on the face the mate lands (GUI click semantics).
  asm.solve(True) solves; the solver moves links, not base parts.
"""

import contextlib
import logging
import math

import FreeCAD as App
import Part

logger = logging.getLogger("CADPilot")

ASSEMBLY_NAME = "MCP_Assembly"

_JOINT_MODS = None


def _joint_mods():
    """Lazy-import Assembly workbench python modules (heavy first import)."""
    global _JOINT_MODS
    if _JOINT_MODS is None:
        import JointObject
        import UtilsAssembly

        _JOINT_MODS = (JointObject, UtilsAssembly)
    return _JOINT_MODS


def _joint_index(joint_type: str) -> int:
    JointObject, _ = _joint_mods()
    names = [n.lower() for n in JointObject.JointTypes]
    if joint_type.lower() not in names:
        raise ValueError(f"unknown joint type {joint_type!r}; allowed: {names}")
    return names.index(joint_type.lower())


def _placement_to_dict(pl) -> dict:
    ax = pl.Rotation.Axis
    return {
        "Base": {"x": pl.Base.x, "y": pl.Base.y, "z": pl.Base.z},
        "Rotation": {
            "Axis": {"x": ax.x, "y": ax.y, "z": ax.z},
            "Angle": math.degrees(pl.Rotation.Angle),
        },
    }


def _dict_to_placement(d: dict):
    ax = d["Rotation"]["Axis"]
    return App.Placement(
        App.Vector(d["Base"]["x"], d["Base"]["y"], d["Base"]["z"]),
        App.Rotation(App.Vector(ax["x"], ax["y"], ax["z"]), d["Rotation"]["Angle"]),
    )


def _get_assembly(doc):
    asm = doc.getObject(ASSEMBLY_NAME)
    if asm is None:
        raise ValueError(f"assembly {ASSEMBLY_NAME} not found; run 'start' first")
    return asm


def _get_link(doc, part_name: str):
    link = doc.getObject(f"L_{part_name}")
    if link is None:
        raise ValueError(f"component {part_name!r} not in assembly")
    return link


def _wrap_link(doc, asm, part_name: str):
    """Part -> App::Link component. The link takes over the Placement; the
    original part is reset to identity and hidden (no double rendering)."""
    part = doc.getObject(part_name)
    if part is None:
        raise ValueError(f"part {part_name!r} not found in document")
    if doc.getObject(f"L_{part_name}") is not None:
        raise ValueError(f"{part_name!r} is already a component")
    link = doc.addObject("App::Link", f"L_{part_name}")
    link.LinkedObject = part
    link.Placement = part.Placement
    part.Placement = App.Placement()
    asm.addObject(link)
    with contextlib.suppress(Exception):
        part.ViewObject.Visibility = False
    return link


def _nearest_vertex_name(shape, face_name: str, pt) -> str:
    face = shape.getElement(face_name)
    face_pts = [v.Point for v in face.Vertexes]
    best, bd = None, 1e18
    for i, v in enumerate(shape.Vertexes):
        if not any(v.Point.distanceToPoint(fp) < 1e-6 for fp in face_pts):
            continue
        d = v.Point.distanceToPoint(pt)
        if d < bd:
            bd, best = d, f"Vertex{i + 1}"
    if best is None:
        raise ValueError(f"no vertex of {face_name} found")
    return best


def _global_shape(link):
    """link.Shape is already GLOBAL (the placed shape).

    NOTE the asymmetry: UtilsAssembly.findPlacement / joint References work
    on the linked part's LOCAL shape (identity frame), while link.Shape is
    placement-transformed. Geometry queries in world space use link.Shape
    directly; composing with link.Placement again would double-transform.
    """
    return link.Shape


def _local_shape(doc, link):
    """The linked part's identity-frame shape (the frame joints reason in)."""
    part = link.LinkedObject
    return part.Shape if part is not None else link.Shape


def _resolve_ref(doc, ref: dict):
    """Ref dict -> (link, ['FaceN', 'VertexM']).

    face   — direct; contact point = 'point_on_face' hint (global) or face CoM.
    anchor — named anchor from assembly_ops; global pos.
    point  — global pos; nearest planar face within 1 mm is used.
    Probing happens against link.Shape (global); face/vertex NAMES are
    frame-agnostic and valid for the solver's local-frame references.
    """
    link = _get_link(doc, ref["part"])
    shape = link.Shape
    if "face" in ref:
        face_name = ref["face"]
        if ref.get("point_on_face") is not None:
            pt = App.Vector(*ref["point_on_face"])
        else:
            pt = shape.getElement(face_name).CenterOfMass
        return (link, [face_name, _nearest_vertex_name(shape, face_name, pt)])
    if "anchor" in ref:
        from . import assembly_ops as aops

        pos, _dir = aops._resolve_anchor(doc.getObject(ref["part"]), ref["anchor"])
        pt = pos
    else:
        pt = App.Vector(*ref["point"])
    best, bd = None, 1.0  # 1 mm tolerance band around the part surface
    probe = Part.Vertex(pt)
    for fi, f in enumerate(shape.Faces):
        if f.Surface.TypeId != "Part::GeomPlane":
            continue
        try:
            d = f.distToShape(probe)[0]
        except Exception:
            continue
        if d < bd:
            bd, best = d, f"Face{fi + 1}"
    if best is None:
        raise ValueError(f"no planar face within 1 mm of the anchor/point on {ref['part']!r}")
    return (link, [best, _nearest_vertex_name(shape, best, pt)])


def _make_joint(asm, joint_type: str, ref_a, ref_b, name: str = ""):
    JointObject, UtilsAssembly = _joint_mods()
    jg = UtilsAssembly.getJointGroup(asm)
    j = jg.newObject("App::FeaturePython", name or "J_MCP")
    JointObject.Joint(j, _joint_index(joint_type))
    JointObject.ViewProviderJoint(j.ViewObject)
    j.Reference1 = ref_a
    j.Placement1 = j.Proxy.findPlacement(j, j.Reference1, 0)
    j.Reference2 = ref_b
    j.Placement2 = j.Proxy.findPlacement(j, j.Reference2, 1)
    return j


def _residual(j):
    """Geometric-truth residual: (face-to-face distance mm, normal angle deg).

    Measured directly between the two referenced faces on link.Shape
    (GLOBAL placed shapes) — immune to the JCS frame asymmetry between
    UtilsAssembly.findPlacement (local) and link placements (global).
    For non-planar faces the angular part is 0 (distance still valid).
    """
    try:
        fa = j.Reference1[0].Shape.getElement(j.Reference1[1][0])
        fb = j.Reference2[0].Shape.getElement(j.Reference2[1][0])
    except Exception:
        return -1.0, -1.0
    try:
        mm = fa.distToShape(fb)[0]
    except Exception:
        mm = -1.0
    deg = 0.0
    if fa.Surface.TypeId == "Part::GeomPlane" and fb.Surface.TypeId == "Part::GeomPlane":
        na = fa.normalAt(*fa.Surface.parameter(fa.CenterOfMass))
        nb = fb.normalAt(*fb.Surface.parameter(fb.CenterOfMass))
        dot = max(-1.0, min(1.0, na.dot(nb)))
        ang = math.degrees(math.acos(dot))
        deg = min(ang, 180.0 - ang)  # opposing normals == touching faces
    return round(mm, 4), round(deg, 3)


def _joints_of(asm):
    return [o for o in asm.Joints] if asm.Joints else []


def _solve_converged(doc, asm) -> None:
    """Single solve + recompute. Repeated solve(True) passes corrupt the
    solver's storePrev state (observed: deterministic ~40 mm drift on a
    3-link chain); one pass after a proper preSolve converges correctly."""
    asm.solve(True)
    doc.recompute()


# ---------------------------------------------------------------- operations


def _op_start(doc, spec: dict) -> dict:
    JointObject, UtilsAssembly = _joint_mods()
    if doc.getObject(ASSEMBLY_NAME) is not None:
        raise ValueError(f"{ASSEMBLY_NAME} already exists; complete/delete it first")
    asm = doc.addObject("Assembly::AssemblyObject", ASSEMBLY_NAME)
    asm.Type = "Assembly"  # without this the solver treats it as non-assembly
    link = _wrap_link(doc, asm, spec["ground"])
    jg = UtilsAssembly.getJointGroup(asm)
    ground = jg.newObject("App::FeaturePython", "GroundedJoint")
    JointObject.GroundedJoint(ground, link)
    JointObject.ViewProviderGroundedJoint(ground.ViewObject)
    doc.recompute()
    return {"assembly": asm.Name, "joint_group": jg.Name, "ground_link": link.Name}


def _op_add_component(doc, spec: dict) -> dict:
    asm = _get_assembly(doc)
    link = _wrap_link(doc, asm, spec["part"])
    doc.recompute()
    return {"link": link.Name, "placement": _placement_to_dict(link.Placement)}


def _op_mate(doc, spec: dict) -> dict:
    asm = _get_assembly(doc)
    ref_a = _resolve_ref(doc, spec["a"])
    ref_b = _resolve_ref(doc, spec["b"])
    moved_link = ref_a[0]
    pre_placement = _placement_to_dict(moved_link.Placement)
    j = _make_joint(asm, spec["joint"], ref_a, ref_b, spec.get("name") or "")
    # GUI-equivalent mating: preSolve (matchJCS) positions the moving part
    # AND its downstream children with normals opposing; the final solve
    # then locks the chain. Skipping preSolve lands mates with faces
    # perpendicular (JCS coincide but faces don't).
    JointObject, _ = _joint_mods()
    if j.JointType in JointObject.JointUsingPreSolve:
        j.Proxy.preSolve(j)
    asm.solve(True)
    doc.recompute()
    mm, deg = _residual(j)
    res = {
        "joint": j.Name,
        "residual_mm": mm,
        "residual_deg": deg,
        "moved_link": moved_link.Name,
        "pre_placement": pre_placement,
        "moved_to": _placement_to_dict(moved_link.Placement),
    }
    trim = spec.get("trim")
    if trim:
        from . import trim_ops

        inserted = spec["a"]["part"]
        base = spec["b"]["part"]
        t = trim_ops.apply_trim(doc, inserted, base, trim["winner"])
        if t:
            res["trim"] = t
    return res


def _op_solve(doc, _spec: dict) -> dict:
    asm = _get_assembly(doc)
    _solve_converged(doc, asm)
    return {
        "joints": [
            {
                "name": j.Name,
                "residual_mm": _residual(j)[0],
                "residual_deg": _residual(j)[1],
                "type": j.JointType,
            }
            for j in _joints_of(asm)
        ]
    }


def _op_unmate(doc, spec: dict) -> dict:
    _get_assembly(doc)
    j = doc.getObject(spec["joint"])
    if j is None:
        raise ValueError(f"joint {spec['joint']!r} not found")
    doc.removeObject(j.Name)
    doc.recompute()
    return {"deleted": spec["joint"]}


def _op_rollback_step(doc, spec: dict) -> dict:
    _get_assembly(doc)
    for name in spec.get("joints_to_delete", []):
        obj = doc.getObject(name)
        if obj is not None:
            doc.removeObject(name)
    for name in spec.get("cuts_to_delete", []):
        obj = doc.getObject(name)
        if obj is not None:
            doc.removeObject(name)
    for link_name, part_name in spec.get("links_repoint", {}).items():
        link = doc.getObject(link_name)
        part = doc.getObject(part_name)
        if link is not None and part is not None:
            link.LinkedObject = part
    for link_name in spec.get("remove_links", []):
        link = doc.getObject(link_name)
        if link is not None:
            # give the original part its placement back before unlinking
            part = link.LinkedObject
            if part is not None:
                part.Placement = link.Placement
                with contextlib.suppress(Exception):
                    part.ViewObject.Visibility = True
            doc.removeObject(link_name)
    for link_name, plc in spec.get("links_restore", {}).items():
        link = doc.getObject(link_name)
        if link is not None:
            link.Placement = _dict_to_placement(plc)
    doc.recompute()
    return {"done": True}


def _op_verify(doc, spec: dict) -> dict:
    asm = _get_assembly(doc)
    out = {
        "joints": [
            {
                "name": j.Name,
                "residual_mm": _residual(j)[0],
                "residual_deg": _residual(j)[1],
                "type": j.JointType,
            }
            for j in _joints_of(asm)
        ]
    }
    try:
        from . import assembly_ops as aops

        audit = aops.verify_assembly(doc.Name)
        out["islands"] = audit.get("islands", [])
        out["interferences"] = audit.get("interferences", [])
        out["floating"] = audit.get("floating", [])
    except Exception as e:
        out["audit_error"] = str(e)
    n = int(spec.get("gap_samples", 8))
    out["gap_profiles"] = [_gap_profile(doc, j, n) for j in _joints_of(asm)]
    return out


def _gap_profile(doc, j, n: int) -> dict:
    """Max PERPENDICULAR gap across the inserted (a-side) face.

    Samples the a-face on a UV grid and measures each point's deviation
    from the mate plane (for planar b-faces) or the b-face itself. Face
    SIZE mismatch (overhang) does not count as a gap — only lifting off
    the mate plane does (the shark-fin-on-sloping-deck problem).
    """
    try:
        link_a, (face_a, _) = j.Reference1[0], j.Reference1[1]
        link_b, (face_b, _) = j.Reference2[0], j.Reference2[1]
        fa = link_a.Shape.getElement(face_a)
        fb = link_b.Shape.getElement(face_b)
        plane_pos, plane_n = None, None
        if fb.Surface.TypeId == "Part::GeomPlane":
            plane_n = fb.normalAt(*fb.Surface.parameter(fb.CenterOfMass))
            plane_pos = fb.CenterOfMass
        u0, u1, v0, v1 = fa.ParameterRange
        worst = 0.0
        for i in range(n + 1):
            for k in range(n + 1):
                pt = fa.Surface.value(u0 + (u1 - u0) * i / n, v0 + (v1 - v0) * k / n)
                try:
                    if plane_n is not None:
                        d = abs((pt - plane_pos).dot(plane_n))
                    else:
                        d = fb.distToShape(Part.Vertex(pt))[0]
                except Exception:
                    continue
                worst = max(worst, d)
        return {"joint": j.Name, "max_gap_mm": round(worst, 3)}
    except Exception as e:
        return {"joint": j.Name, "error": str(e)}


_DISPATCH = {
    "start": _op_start,
    "add_component": _op_add_component,
    "mate": _op_mate,
    "solve": _op_solve,
    "unmate": _op_unmate,
    "rollback_step": _op_rollback_step,
    "verify": _op_verify,
}


def assembly_op(doc, spec: dict) -> dict:
    op = spec.get("operation")
    fn = _DISPATCH.get(op)
    if fn is None:
        raise ValueError(f"unknown assembly operation {op!r}")
    return fn(doc, spec)
