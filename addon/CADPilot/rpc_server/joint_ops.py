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
import math

import FreeCAD as App
import Part

from rpc_server import dbglog

logger = dbglog.get_logger("assembly")

ASSEMBLY_NAME = "MCP_Assembly"

# Joint types that need an AXIS: their refs get an axis note (see
# _axis_ref_notes) so the axis a mate will use is never a guess.
_AXIS_JOINTS = frozenset({"revolute", "cylindrical", "slider"})

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


def _shape_vertex_name(shape, point) -> str | None:
    """The shape-global name of the vertex at ``point`` (None when absent)."""
    best, bd = None, 1e-6
    for i, v in enumerate(shape.Vertexes):
        d = v.Point.distanceToPoint(point)
        if d < bd:
            bd, best = d, f"Vertex{i + 1}"
    return best


def _shape_edge_name(shape, edge) -> str | None:
    """The shape-global name of ``edge`` (a face-local sub-shape)."""
    for i, e in enumerate(shape.Edges):
        if e.Curve.TypeId != edge.Curve.TypeId or abs(e.Length - edge.Length) > 1e-6:
            continue
        if e.CenterOfGravity.distanceToPoint(edge.CenterOfGravity) < 1e-6:
            return f"Edge{i + 1}"
    return None


def _vec3(vec) -> str:
    return "[" + ", ".join(f"{float(v):.2f}" for v in vec) + "]"


def _face_landing(shape, face_name: str, pt=None) -> str:
    """Second sub-element for a face ref — where the joint's JCS lands.

    ``UtilsAssembly.findPlacement`` derives the landing point from the SECOND
    element's type: a Vertex lands at that vertex, a circular/elliptical edge
    lands at its CENTER, and the face's own name lands at the face center.
    Always appending the nearest vertex — the old behaviour — mis-landed every
    circular face, whose only vertex is the OCC seam: two concentric holes
    came out R-r off axis, with residual 0 and no warning. A plain face ref
    (no hint) uses the GUI's own marker — "we add sub_name twice because the
    joint references have element name + vertex name" — so it lands on the
    face center exactly; a hinted/anchor/point ref keeps click semantics and
    snaps to the closest selectable point.
    """
    face = shape.getElement(face_name)
    if pt is None:
        return face_name
    cands = []
    for v in face.Vertexes:
        name = _shape_vertex_name(shape, v.Point)
        if name:
            cands.append((v.Point, name))
    for e in face.Edges:
        if e.Curve.TypeId in ("Part::GeomCircle", "Part::GeomEllipse"):
            name = _shape_edge_name(shape, e)
            if name:
                cands.append((e.Curve.Location, name))
    cands.append((face.CenterOfGravity, face_name))
    return min(cands, key=lambda c: c[0].distanceToPoint(pt))[1]


def _landing_point(link, fe) -> App.Vector | None:
    """Where the ref's JCS lands (mirrors findPlacement's element-type rules)."""
    shape = link.Shape
    name = str(fe[1])
    try:
        if name == str(fe[0]) or name.startswith("Face"):
            return shape.getElement(str(fe[0])).CenterOfGravity
        if name.startswith("Edge"):
            edge = shape.getElement(name)
            if edge.Curve.TypeId in ("Part::GeomCircle", "Part::GeomEllipse"):
                return edge.Curve.Location
            return edge.CenterOfGravity
        return shape.getElement(name).Point
    except Exception:
        return None


def _resolve_ref(doc, ref: dict):
    """Ref dict -> (link, ['FaceN', <landing sub-element>]).

    face   — direct; contact point = 'point_on_face' hint (global) or the face
             center (the marker convention).
    anchor — named anchor from assembly_ops; global pos.
    point  — global pos; nearest planar face within 1 mm is used.
    Probing happens against link.Shape (global); face/vertex NAMES are
    frame-agnostic and valid for the solver's local-frame references.
    """
    link = _get_link(doc, ref["part"])
    shape = link.Shape
    if "face" in ref:
        face_name = ref["face"]
        hint = ref.get("point_on_face")
        pt = App.Vector(*hint) if hint is not None else None
        return (link, [face_name, _face_landing(shape, face_name, pt)])
    if "anchor" in ref:
        from . import assembly_ops as aops

        # _resolve_anchor returns (pos, dir, source, error) in the BASE
        # object's placed frame. After add_component the link owns the
        # placement (the base sits at identity), so probing link.Shape needs
        # the anchor mapped through link.Placement.
        pos, _dir, _src, aerr = aops._resolve_anchor(doc.getObject(ref["part"]), ref["anchor"])
        if aerr:
            raise ValueError(aerr)
        pt = link.Placement.multVec(pos)
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
    return (link, [best, _face_landing(shape, best, pt)])


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


_ROTATIONAL_SURFACES = frozenset({"Part::GeomCylinder", "Part::GeomCone"})


def _axis_of(face):
    """(point, unit direction) of a rotational face's axis, or None.

    A GeomCylinder/GeomCone surface exposes ``Center``/``Axis`` as plain
    Vectors (not a gp_Ax1) — ``Center`` is a point ON the axis.
    """
    try:
        surface = face.Surface
        return App.Vector(surface.Center), App.Vector(surface.Axis)
    except Exception:
        return None


def _residual(j):
    """Geometric-truth residual: (distance mm, angle deg, basis).

    Measured directly between the two referenced faces on link.Shape
    (GLOBAL placed shapes) — immune to the JCS frame asymmetry between
    UtilsAssembly.findPlacement (local) and link placements (global).
    For planar pairs the angle is the normal deviation (opposing normals ==
    touching). For two rotational faces the distance is AXIS-TO-AXIS and the
    angle is the axis deviation: an axle fit's surface distance is its radial
    gap, so a 5 mm pin in a 20 mm bore reported 15 mm — reading like a broken
    mate although the axes were perfectly coaxial (live-verified).
    """
    try:
        fa = j.Reference1[0].Shape.getElement(j.Reference1[1][0])
        fb = j.Reference2[0].Shape.getElement(j.Reference2[1][0])
    except Exception:
        return -1.0, -1.0, "surface"
    try:
        mm = fa.distToShape(fb)[0]
    except Exception:
        mm = -1.0
    deg, basis = 0.0, "surface"
    if fa.Surface.TypeId == "Part::GeomPlane" and fb.Surface.TypeId == "Part::GeomPlane":
        na = fa.normalAt(*fa.Surface.parameter(fa.CenterOfMass))
        nb = fb.normalAt(*fb.Surface.parameter(fb.CenterOfMass))
        dot = max(-1.0, min(1.0, na.dot(nb)))
        ang = math.degrees(math.acos(dot))
        deg = min(ang, 180.0 - ang)  # opposing normals == touching faces
    elif fa.Surface.TypeId in _ROTATIONAL_SURFACES and fb.Surface.TypeId in _ROTATIONAL_SURFACES:
        aa, ab = _axis_of(fa), _axis_of(fb)
        if aa and ab:
            (pa, da), (pb, db) = aa, ab
            cross = da.cross(db)
            if cross.Length > 1e-9:
                mm = abs((pb - pa).dot(cross)) / cross.Length  # skew-line distance
                deg = math.degrees(math.acos(max(-1.0, min(1.0, abs(da.dot(db))))))
            else:
                mm = (pb - pa).cross(da).Length  # parallel: perpendicular distance
                deg = 0.0
            basis = "axis"
    return round(mm, 4), round(deg, 3), basis


def _joints_of(asm):
    return [o for o in asm.Joints] if asm.Joints else []


def _links_of(asm):
    """The component links of an assembly (App::Link children of the group)."""
    return [o for o in getattr(asm, "Group", []) or [] if getattr(o, "TypeId", "") == "App::Link"]


def _same_placement(a, b) -> bool:
    if (a.Base - b.Base).Length > 1e-9:
        return False
    return abs(a.Rotation.multiply(b.Rotation.inverted()).Angle) <= 1e-9


def _joint_link_names(j) -> set:
    """The link names a joint's two references point at."""
    names = set()
    for attr in ("Reference1", "Reference2"):
        ref = getattr(j, attr, None)
        if isinstance(ref, (list, tuple)) and ref and hasattr(ref[0], "Name"):
            names.add(ref[0].Name)
    return names


def _duplicate_joint_warnings(asm, ref_a, ref_b) -> list:
    """Warn when the same link pair is already constrained by another joint.

    Live-verified: a second revolute on the same pair was accepted silently and
    both joints reported residual 0 — an over-constrained assembly with no
    signal. The solver treats both as real constraints, so say so.
    """
    pair = {ref_a[0].Name, ref_b[0].Name}
    existing = [j.Name for j in _joints_of(asm) if _joint_link_names(j) == pair]
    if not existing:
        return []
    return [
        f"another joint ({', '.join(existing)}) already constrains this link pair; both "
        "stay active, so the pair is constrained twice (a redundant/over-constrained "
        "assembly). Use unmate first if the new joint is meant to replace it."
    ]


def _solve_converged(doc, asm) -> None:
    """Single solve + recompute. Repeated solve(True) passes corrupt the
    solver's storePrev state (observed: deterministic ~40 mm drift on a
    3-link chain); one pass after a proper preSolve converges correctly."""
    asm.solve(True)
    doc.recompute()


def _settle_shapes(doc, asm) -> None:
    """Re-derive lazy shape caches INSIDE the current transaction.

    AssemblyObject.Shape (and the App::Link composites) rebuild lazily on
    first access, not on recompute — after a mate/solve moved links, the next
    transaction to READ that Shape (typically a read-only ``verify``/audit)
    absorbs the rebuild write and earns a phantom undo entry, making a
    read-only step claim a transaction (live-verified: verify right after a
    mate produced UndoCount +1; the second verify did not). Reading the
    caches here charges the rebuild to the op that actually caused it; the
    reads cost nothing when the cache is already valid.
    """
    # The read IS the effect; assigning it says so to a reader (and to B018).
    with contextlib.suppress(Exception):
        _ = asm.Shape
    for obj in doc.Objects:
        if obj.TypeId == "App::Link":
            with contextlib.suppress(Exception):
                _ = obj.Shape


# ---------------------------------------------------------------- operations


def _op_start(doc, spec: dict) -> dict:
    JointObject, UtilsAssembly = _joint_mods()
    if doc.getObject(ASSEMBLY_NAME) is not None:
        raise ValueError(
            f'{ASSEMBLY_NAME} already exists and no active MCP session owns it '
            "(complete never deletes the assembly; it stays as the model's joints). "
            "To start over, delete it via execute_code first: "
            "doc = App.ActiveDocument; "
            "asm = doc.getObject('MCP_Assembly'); "
            "[doc.removeObject(o.Name) for o in list(asm.OutList)]; "
            "doc.removeObject('MCP_Assembly'); doc.recompute()"
        )
    asm = doc.addObject("Assembly::AssemblyObject", ASSEMBLY_NAME)
    asm.Type = "Assembly"  # without this the solver treats it as non-assembly
    link = _wrap_link(doc, asm, spec["ground"])
    jg = UtilsAssembly.getJointGroup(asm)
    ground = jg.newObject("App::FeaturePython", "GroundedJoint")
    JointObject.GroundedJoint(ground, link)
    JointObject.ViewProviderGroundedJoint(ground.ViewObject)
    doc.recompute()
    _settle_shapes(doc, asm)
    return {
        "assembly": asm.Name,
        "joint_group": jg.Name,
        "ground_link": link.Name,
        "ground_joint": ground.Name,
    }


def _op_add_component(doc, spec: dict) -> dict:
    asm = _get_assembly(doc)
    link = _wrap_link(doc, asm, spec["part"])
    doc.recompute()
    _settle_shapes(doc, asm)
    return {"link": link.Name, "placement": _placement_to_dict(link.Placement)}


def _landing_warnings(doc, spec: dict, ref_a, ref_b) -> list[str]:
    """Warn when a mate's landing sits far from the point the caller aimed at.

    A plain face ref now lands on the face center by construction (the
    face-name marker), so only hint/anchor/point refs can deviate: they snap
    to the closest selectable point (vertex, circular-edge center, face
    center), and on a symmetric face that choice is arbitrary — a plate's top
    face lands at whichever corner is nearest the anchor.
    """
    warnings = []
    for side, resolved in (("a", ref_a), ("b", ref_b)):
        r = spec[side]
        link, fe = resolved
        if "face" in r:
            if r.get("point_on_face") is None:
                continue  # lands on the face center exactly
            intent = App.Vector(*r["point_on_face"])
            remedy = "aim point_on_face at a vertex or a circular edge center to land there"
        elif "anchor" in r:
            from . import assembly_ops as aops

            pos, _d, _s, aerr = aops._resolve_anchor(doc.getObject(r["part"]), r["anchor"])
            if aerr:
                continue
            intent = link.Placement.multVec(pos)
            remedy = "use a face ref with point_on_face, or an anchor sitting on the wanted spot"
        else:
            intent = App.Vector(*r["point"])
            remedy = "use a face ref with point_on_face, or an anchor sitting on the wanted spot"
        landing = _landing_point(link, fe)
        if landing is None:
            continue
        d = landing.distanceToPoint(intent)
        if d > 0.5:  # rigid measure — placement-invariant, safe pre-solve
            warnings.append(
                f"'{r['part']}' {fe[0]} lands on {fe[1]} at {_vec3(landing)}, {d:.2f}mm from "
                f"the ref's intent point {_vec3(intent)}; {remedy}."
            )
    return warnings


def _axis_ref_notes(ref_a, ref_b, joint_type: str) -> list[str]:
    """Say which axis an axis-requiring joint will actually use.

    Ref landings used to be nearest-vertex, which on a cylinder landed the
    parts TANGENT (live, before the face-center landing: a revolute pin/bore
    mate came out 2.1 mm off the bore axis = bore_r - pin_r, reported as
    residual 0) and this function refused such refs outright. A cylindrical
    face now lands on its AXIS — the same thing FreeCAD's own click rules do —
    so an axle-in-hole joint works and the honest output is to name the axis
    the joint will use rather than to block it.
    """
    if str(joint_type).lower() not in _AXIS_JOINTS:
        return []
    notes = []
    for side, ref in (("a", ref_a), ("b", ref_b)):
        link, names = ref
        try:
            face = link.Shape.getElement(names[0])
        except Exception:
            continue
        if face.Surface.TypeId == "Part::GeomPlane":
            continue
        axis = _axis_of(face)
        kind = face.Surface.TypeId.rsplit("::", 1)[-1]
        if axis is None:
            notes.append(
                f"side {side}: {joint_type} on the {kind} face {names[0]} has no axis of its "
                "own; the joint uses the face-center frame instead."
            )
            continue
        pt, direction = axis
        notes.append(
            f"side {side}: the joint's axis is the {kind} axis through {_vec3(pt)} "
            f"(direction {_vec3(direction)}); residual_mm is measured axis-to-axis for "
            "two rotational faces."
        )
    return notes


def _op_mate(doc, spec: dict) -> dict:
    asm = _get_assembly(doc)
    ref_a = _resolve_ref(doc, spec["a"])
    ref_b = _resolve_ref(doc, spec["b"])
    landing_warnings = _axis_ref_notes(ref_a, ref_b, spec["joint"])
    landing_warnings += _landing_warnings(doc, spec, ref_a, ref_b)
    landing_warnings += _duplicate_joint_warnings(asm, ref_a, ref_b)
    # Which link the solver MOVES is its own choice (usually b's), not a's: the
    # old record named ref_a's link, so a rollback restored a part that had
    # never moved and left the moved one where the solve put it (live: a hinge
    # lid stayed at z 70..90 after rollback instead of returning to 150..170).
    # Snapshot every component link, then diff after the solve.
    links_before = {
        lnk.Name: App.Placement(lnk.Placement.Base, lnk.Placement.Rotation)
        for lnk in _links_of(asm)
    }
    j = _make_joint(asm, spec["joint"], ref_a, ref_b, spec.get("name") or "")
    # GUI-equivalent mating: preSolve (matchJCS) positions the moving part
    # AND its downstream children with normals opposing; the final solve
    # then locks the chain. Skipping preSolve lands mates with faces
    # perpendicular (JCS coincide but faces don't).
    JointObject, _ = _joint_mods()
    used_pre_solve = j.JointType in JointObject.JointUsingPreSolve
    if used_pre_solve:
        j.Proxy.preSolve(j)
    asm.solve(True)
    doc.recompute()
    mm, deg, basis = _residual(j)
    moved_links = {}
    for lnk in _links_of(asm):
        before = links_before.get(lnk.Name)
        if before is not None and not _same_placement(before, lnk.Placement):
            moved_links[lnk.Name] = {
                "pre": _placement_to_dict(before),
                "to": _placement_to_dict(lnk.Placement),
            }
    # Legacy single-link fields (an older MCP client records only these) must
    # name the link that ACTUALLY moved, b's side preferred when both did. A
    # mate the solver satisfied without moving anything reports NO moved link:
    # naming ref_a made the fields contradict their own evidence (moved_links
    # empty, pre == moved_to on an untouched ground link) and told the MCP
    # recorder to "restore" a part that never left.
    if ref_b[0].Name in moved_links:
        primary = ref_b[0].Name
    elif ref_a[0].Name in moved_links:
        primary = ref_a[0].Name
    elif moved_links:
        primary = next(iter(moved_links))
    else:
        primary = None
        landing_warnings.append(
            "the mate was satisfied without moving any component (the parts were already "
            "in the requested relationship); no placement was recorded for rollback."
        )
    entry = moved_links.get(primary) if primary else None
    # An axis joint aligns the two JCS AXIS POINTS, so a PARTIAL curved face
    # (its reference point is the surface's own axis base, not the face center)
    # can slide the part along the common axis by tens of mm while the residual
    # reads 0 — live: a revolute on two hinge barrels translated the lid
    # -20.62 mm along X and buried the barrels in each other; the mate itself
    # said nothing (a later verify found 4417 mm^3 of interference).
    axial_slide = None
    if str(spec["joint"]).lower() in _AXIS_JOINTS and entry is not None:
        axis_dir = None
        for link, names in (ref_a, ref_b):
            with contextlib.suppress(Exception):
                found = _axis_of(link.Shape.getElement(names[0]))
                if found is not None and axis_dir is None:
                    axis_dir = found[1]
        if axis_dir is not None:
            pre = _dict_to_placement(moved_links[primary]["pre"])
            post = _dict_to_placement(moved_links[primary]["to"])
            axial_slide = round((post.Base - pre.Base).dot(axis_dir), 4)
            if abs(axial_slide) > 2.0:
                landing_warnings.append(
                    f"the joint aligned the two axis reference points, which slid "
                    f"'{primary}' {axial_slide:.2f} mm ALONG the joint axis (a partial "
                    "curved face's reference point is the surface's own axis base, not "
                    "its face center) — give both refs a point_on_face to control the "
                    f"axial position. axial_slide_mm reports the displacement."
                )
    # preSolve vs. solve-only is the difference between a correct mate and
    # faces landing perpendicular; record which path ran and how it settled.
    logger.debug(
        "mate %s (%s): preSolve=%s residual=%.3fmm/%.2fdeg moved=%s",
        j.Name,
        spec["joint"],
        used_pre_solve,
        mm,
        deg,
        list(moved_links),
    )
    res = {
        "joint": j.Name,
        "residual_mm": mm,
        "residual_deg": deg,
        "residual_basis": basis,
        "moved_link": primary,
        "moved_links": moved_links,
        "landing": {"a": list(ref_a[1]), "b": list(ref_b[1])},
        "warnings": landing_warnings,
    }
    # Where the JCS landed once the solver settled — the number to check a mate
    # against (a residual of 0 only says the two JCS frames agree, not that the
    # frames landed where the caller meant).
    pts = {"a": _landing_point(ref_a[0], ref_a[1]), "b": _landing_point(ref_b[0], ref_b[1])}
    if all(v is not None for v in pts.values()):
        res["landing_points"] = {k: _vec3(v) for k, v in pts.items()}
    if entry is not None:
        res["pre_placement"] = entry["pre"]
        res["moved_to"] = entry["to"]
    if axial_slide is not None:
        res["axial_slide_mm"] = axial_slide
    trim = spec.get("trim")
    if trim:
        from . import trim_ops

        inserted = spec["a"]["part"]
        base = spec["b"]["part"]
        t = trim_ops.apply_trim(doc, inserted, base, trim["winner"])
        if t:
            res["trim"] = t
        else:
            # apply_trim returns None for a sub-1 mm³ graze — say so instead
            # of silently dropping the requested trim from the result.
            res["warnings"] = res["warnings"] + [
                f"trim requested but '{inserted}' and '{base}' overlap by less than 1 mm³ — nothing trimmed"
            ]
    _settle_shapes(doc, asm)
    return res


def _op_solve(doc, _spec: dict) -> dict:
    asm = _get_assembly(doc)
    _solve_converged(doc, asm)
    _settle_shapes(doc, asm)
    return {
        "joints": [
            {
                "name": j.Name,
                "residual_mm": _residual(j)[0],
                "residual_deg": _residual(j)[1],
                "residual_basis": _residual(j)[2],
                "type": j.JointType,
            }
            for j in _joints_of(asm)
        ]
    }


def _op_unmate(doc, spec: dict) -> dict:
    asm = _get_assembly(doc)
    j = doc.getObject(spec["joint"])
    if j is None:
        raise ValueError(f"joint {spec['joint']!r} not found")
    doc.removeObject(j.Name)
    doc.recompute()
    _settle_shapes(doc, asm)
    return {"deleted": spec["joint"]}


def _op_rollback_step(doc, spec: dict) -> dict:
    asm = _get_assembly(doc)
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
    # Restore link placements BEFORE removing links: remove_links hands the
    # link's CURRENT placement back to the part, so restoring afterwards (when
    # the link is already gone) would silently leak the post-mate placement
    # into the base part instead of the pre-mate one.
    for link_name, plc in spec.get("links_restore", {}).items():
        link = doc.getObject(link_name)
        if link is not None:
            link.Placement = _dict_to_placement(plc)
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
    asm_name = spec.get("remove_assembly")
    if asm_name and doc.getObject(asm_name) is not None:
        # Tearing the assembly down (rollback across 'start'): its remaining
        # children — the joint group above all — go with it, or the document is
        # left with orphans that make a fresh start() fail.
        for child in [
            o for o in doc.Objects if any(i.Name == asm_name for i in getattr(o, "InList", []))
        ]:
            with contextlib.suppress(Exception):
                doc.removeObject(child.Name)
        with contextlib.suppress(Exception):
            doc.removeObject(asm_name)
    doc.recompute()
    _settle_shapes(doc, asm)
    out = {"done": True}
    # Consistency glance: a rollback restores the placements the ASSEMBLY
    # journal recorded, and a placement change the journal never saw (a plain
    # cad move on a link, a manual GUI drag) is not among them — the parts can
    # end up detached or overlapping while the rollback still reports success
    # (live: a lamp arm rolled back onto a base link that had been moved
    # afterwards). Report the document's actual state instead of leaving it to
    # the caller to discover.
    try:
        from . import assembly_ops as aops

        audit = aops.verify_assembly(doc.Name)
        s = audit.get("summary") or {}
        out["verify"] = {
            k: s[k]
            for k in ("island_count", "floating_count", "interference_count", "component_count")
            if k in s
        }
        problems = []
        if s.get("island_count", 0) > 1:
            problems.append(f"{s['island_count']} disconnected islands")
        if s.get("floating_count", 0):
            problems.append(f"{s['floating_count']} floating objects")
        if s.get("interference_count", 0):
            problems.append(f"{s['interference_count']} interferences")
        if problems:
            out["warnings"] = [
                "the document is not consistent after this rollback: "
                + ", ".join(problems)
                + " — a placement change made outside the assembly journal "
                "(e.g. a cad move on a link) is not restored by rollback."
            ]
    except Exception as e:
        out["verify_error"] = str(e)
    return out


def _op_verify(doc, spec: dict) -> dict:
    asm = _get_assembly(doc)
    out = {
        "joints": [
            {
                "name": j.Name,
                "residual_mm": _residual(j)[0],
                "residual_deg": _residual(j)[1],
                "residual_basis": _residual(j)[2],
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
    try:
        return fn(doc, spec)
    except Exception:
        # The RPC layer reports a bare string to the client; the traceback has
        # to be captured here or it is lost (the solver errors are opaque).
        logger.error("assembly op %r failed: %s", op, spec, exc_info=True)
        raise
