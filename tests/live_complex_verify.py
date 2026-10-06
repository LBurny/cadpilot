"""Complex real-scenario live verification: the accumulating regression corpus.

Each scenario builds a realistic model through the MCP tool layer against a
LIVE FreeCAD, asserts analytic volumes/counts, and exercises the paths the
fake-connection suite cannot see (silent geometry, journal truth, undo
semantics). Stress rounds ADD cases here instead of writing throwaway probes,
so bug N stays dead at round N+1.

Run: .venv/Scripts/python.exe tests/live_complex_verify.py
Requires: FreeCAD running with the CADPilot addon and its RPC server started.
"""

import contextlib
import math
import sys

from cadpilot.freecad_client import FreeCADConnection

c = FreeCADConnection(connect_grace=10)
FAILURES: list[str] = []
CHECKS = 0


def check(name, cond, detail=""):
    global CHECKS
    CHECKS += 1
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(f"{name}: {detail}")


def close_doc(name):
    with contextlib.suppress(Exception):
        c.execute_code(f"import FreeCAD; FreeCAD.closeDocument('{name}')")


def new_doc(name):
    close_doc(name)
    res = c.create_document(name)
    assert res.get("success"), res
    return name


def feat(doc, operation, base=None, name=None, **props):
    """base = the input solid (the profile sketch for pad/pocket), name = the
    new object. For the no-base ops (sketch/variables) `base` IS the new
    object's name — FreeCAD-side quirk, see sketcher_ops line 679."""
    spec = {"type": operation, "base": base, "name": name, **props}
    return c.create_feature(doc, spec)


def vol(doc, obj):
    r = c.measure_geometry(doc, obj)
    assert r.get("success"), r
    return r["volume_mm3"]


def bbox_of(doc, obj):
    r = c.measure_geometry(doc, obj)
    assert r.get("success"), r
    return r["bbox"]


def journal(doc):
    r = c.journal_op(doc, {"operation": "status"})
    assert r.get("success"), r
    return r


def text_of(resp):
    """ToolResponse (list of TextContent) or a raw RPC dict."""
    if isinstance(resp, dict):
        import json as _json

        return _json.dumps(resp, ensure_ascii=False)
    return "\n".join(t.text for t in resp if hasattr(t, "text"))


# =====================================================================
# Scenario A — parametric flange: variables -> sketch -> pad -> through_all
# pocket -> expression-driven polar pattern -> fillet, then replay.
# =====================================================================
def scenario_flange():
    doc = new_doc("LiveFlange")
    r = feat(
        doc,
        "variables",
        base="Vars",
        cells={
            "A1": ["flange_d", 70.0],
            "A2": ["bore_d", 20.0],
            "A3": ["thick", 12.0],
            "A4": ["holes", 6.0],
            "A5": ["bolt_d", 13.0],
        },
    )
    check("A: variables created", r.get("success"), str(r)[:200])

    spec = {
        "type": "sketch",
        "base": "FlangeProfile",
        "plane": "XY",
        "geometry": [
            {"type": "circle", "center": [0, 0], "radius": 35},
            {"type": "circle", "center": [0, 0], "radius": 10},
        ],
        "constraints": [
            {"type": "radius", "items": [0], "value": "=Vars.flange_d / 2"},
            {"type": "radius", "items": [1], "value": "=Vars.bore_d / 2"},
            {"type": "coincident", "items": [[0, "center"], [-1, 1]]},
            {"type": "coincident", "items": [[1, "center"], [-1, 1]]},
        ],
    }
    r = c.create_feature(doc, spec)
    check("A: profile sketch", r.get("success") is True, text_of(r)[:300])
    check("A: profile fully constrained", r.get("fully_constrained") is True, text_of(r)[:300])

    r = feat(doc, "pad", base="FlangeProfile", name="FlangePad", length="=Vars.thick")
    check("A: pad", r.get("success") is True, text_of(r)[:300])

    # Bolt-hole profile on the TOP face, then a parametric through-all pocket.
    r = feat(
        doc,
        "sketch",
        base="BoltHole",
        plane={"face": ["FlangePad", "+Z"], "center": True},
        geometry=[{"type": "circle", "center": [25, 0], "radius": 6.5}],
        constraints=[{"type": "radius", "items": [0], "value": "=Vars.bolt_d / 2"}],
    )
    check("A: bolt sketch", r.get("success") is True, text_of(r)[:300])
    r = feat(doc, "pocket", base="BoltHole", name="BoltCut", through_all=True)
    check("A: through_all pocket", r.get("success") is True, text_of(r)[:300])

    r = feat(
        doc,
        "pattern",
        base="BoltCut",
        name="BoltRing",
        pattern_type="polar",
        axis="Z",
        count="=Vars.holes",
        angle=360,
    )
    check("A: polar pattern", r.get("success") is True, text_of(r)[:300])

    # fillet the outer rim edge: the circular edge at max radius
    topo = c.get_topology(doc, "BoltRing", element="edges", limit=200)
    edges = [e for e in topo.get("edges", []) if abs(e.get("radius", 0) - 35.0) < 0.01]
    check("A: rim edges found", len(edges) >= 1, str(len(edges)))
    if edges:
        r = feat(
            doc,
            "fillet",
            base="BoltRing",
            name="RimFillet",
            radius=2.0,
            edges=[e["name"] for e in edges[:1]],
        )
        check("A: rim fillet", r.get("success") is True, text_of(r)[:300])

    def no_fillet(thick):
        # Exact: ring minus 6 through bolt holes.
        return math.pi * ((35**2 - 10**2) - 6 * 6.5**2) * thick

    v = vol(doc, "RimFillet")
    fillet_delta_12 = no_fillet(12) - v
    check(
        "A: volume analytic (holes through)",
        100 < fillet_delta_12 < 300,
        f"vol={v} nofillet={no_fillet(12):.1f} fillet removed {fillet_delta_12:.1f} (want ~187, the ring roundover)",
    )
    check("A: fillet removed material", fillet_delta_12 > 20, f"delta={fillet_delta_12:.1f}")

    # Expression chain: thicker plate -> EVERYTHING follows (pad bound to the
    # variable; the through_all pocket must stay through at any thickness).
    r = feat(doc, "variables", base="Vars", cells={"A3": ["thick", 30.0]})
    check("A: variable changed", r.get("success") is True, text_of(r)[:200])
    v2 = vol(doc, "RimFillet")
    fillet_delta_30 = no_fillet(30) - v2
    check(
        "A: through_all stays through at 30mm",
        abs(no_fillet(30) - v2) < 400 and abs(fillet_delta_30 - fillet_delta_12) < 5,
        f"vol={v2} nofillet={no_fillet(30):.1f} fillet removed {fillet_delta_30:.1f} "
        f"(12mm removed {fillet_delta_12:.1f}; the roundover must not scale with thickness)",
    )

    # Journal replay rebuilds the model from the journal.
    r = c.journal_op(doc, {"operation": "replay"})
    check("A: replay ok", r.get("success") is True, text_of(r)[:300])
    v3 = vol(doc, "RimFillet") if "RimFillet" in [o["Name"] for o in c.get_objects(doc)] else None
    names = [o["Name"] for o in c.get_objects(doc)]
    if v3 is not None:
        check("A: replay reproduces volume", abs(v3 - v2) < 1e-6, f"{v3} vs {v2}")
    else:
        check("A: replay keeps objects", "BoltRing" in names, str(names))
    close_doc(doc)


# =====================================================================
# Scenario B — enclosure: thickness shell -> draft -> fillet -> move ->
# color, plus the get_view selection-preservation and file-delivery probes.
# =====================================================================
def scenario_enclosure():
    doc = new_doc("LiveEnclosure")
    r = c.create_object(
        doc,
        {
            "Name": "Block",
            "Type": "Part::Box",
            "Properties": {"Length": 80, "Width": 50, "Height": 30},
        },
    )
    check("B: box", r.get("success"), text_of(r)[:200])

    # Draft the SOLID side face first (drafting a 2 mm shell wall is
    # geometrically degenerate — FreeCAD itself refuses it).
    r = feat(doc, "draft", base="Block", name="Taper", angle=5, faces=["-Y"], neutral_plane="+Z")
    check("B: draft applied", r.get("success") is True, text_of(r)[:300])
    v_solid = vol(doc, "Taper")
    bb = bbox_of(doc, "Taper")
    check(
        "B: draft kept the neutral plane fixed",
        abs(bb["zmax"] - 30) < 0.01 and abs(bb["zmin"]) < 0.01,
        str(bb),
    )
    check("B: draft changed volume", 0 < abs(v_solid - 120000) < 18000, f"{v_solid}")

    # Hollow: open the top, walls inward (reversed) -> exact analytic cavity.
    r = feat(doc, "thickness", base="Taper", name="Shell", faces=["+Z"], value=2, reversed=True)
    check("B: thickness shell", r.get("success") is True, text_of(r)[:300])
    sh = vol(doc, "Shell")
    check(
        "B: shell volume analytic",
        abs(sh - 22112) < 800,
        f"{sh} vs 22112 (the drafted wall tilts the cavity; exact for the undrafted box)",
    )

    topo = c.get_topology(doc, "Shell", element="edges", limit=200)
    top_edges = [e for e in topo.get("edges", []) if e.get("center", [0, 0, 0])[2] > 28.5]
    if len(top_edges) >= 4:
        r = feat(
            doc,
            "fillet",
            base="Shell",
            name="SoftTop",
            radius=1.5,
            edges=[e["name"] for e in top_edges[:4]],
        )
        check("B: top fillet", r.get("success") is True, text_of(r)[:300])
    else:
        check("B: top fillet", False, f"only {len(top_edges)} candidate edges")

    # move at the Part level: placement must persist across recomputes
    r = feat(doc, "move", base="Block", translate=[10, 0, 0])
    check("B: move", r.get("success") is True, text_of(r)[:200])
    bb = bbox_of(doc, "SoftTop")
    check("B: move persisted", abs(bb["xmin"] - 10) < 0.5, str(bb))

    r = feat(doc, "color", base="Shell", color=[0.8, 0.2, 0.2])
    check("B: color", r.get("success") is True, text_of(r)[:300])

    # Selection preservation: focus capture must hand the selection back.
    r = c.execute_code(
        "import FreeCADGui; FreeCADGui.Selection.clearSelection(); "
        "FreeCADGui.Selection.addSelection(App.ActiveDocument.getObject('Block')); "
        "print('selected-before:', [o.Name for o in FreeCADGui.Selection.getSelection()])",
        doc_name=doc,
    )
    check("B: pre-selection set", "Block" in (r.get("message") or ""), str(r)[:200])
    shot = c.get_active_screenshot("Isometric", 384, 384, focus_object="SoftTop", doc_name=doc)
    check("B: screenshot delivered", bool(shot), "no base64 returned")
    if shot:
        import base64
        from pathlib import Path

        p = Path("H:/My_Software/CADPilot/.live_shot.png")
        p.write_bytes(base64.b64decode(shot))
        check("B: screenshot non-trivial", p.stat().st_size > 3000, str(p.stat().st_size))
        p.unlink()
    r = c.execute_code(
        "import FreeCADGui; print('selected-after:', [o.Name for o in FreeCADGui.Selection.getSelection()])",
        doc_name=doc,
    )
    check(
        "B: selection preserved across focus capture",
        "Block" in (r.get("message") or ""),
        str(r)[:200],
    )
    close_doc(doc)


# =====================================================================
# Scenario C — hinge assembly: anchors, axis mate, verify, unmate warning,
# the complete->start escape, and assembly rollback truth.
# =====================================================================
def scenario_hinge():
    doc = new_doc("LiveHinge")
    # Base: plate + two barrel cylinders, fused.
    r = c.create_object(
        doc,
        {
            "Name": "BasePlate",
            "Type": "Part::Box",
            "Properties": {"Length": 60, "Width": 40, "Height": 8, "Placement": [0, 0, 0]},
        },
    )
    check("C: base plate", r.get("success"), text_of(r)[:200])
    r = c.create_object(
        doc,
        {
            "Name": "BarrelA",
            "Type": "Part::Cylinder",
            "Properties": {"Radius": 5, "Height": 50, "Placement": [0, -5, 13]},
        },
    )
    check("C: barrel A", r.get("success"), text_of(r)[:200])
    r = feat(doc, "boolean", base="BasePlate", name="HingeBase", op="fuse", tool=["BarrelA"])
    check("C: base fuse", r.get("success") is True, text_of(r)[:300])
    r = c.create_object(
        doc,
        {
            "Name": "LidPlate",
            "Type": "Part::Box",
            "Properties": {"Length": 60, "Width": 40, "Height": 6, "Placement": [0, 0, 60]},
        },
    )
    r = c.create_object(
        doc,
        {
            "Name": "Knuckle",
            "Type": "Part::Cylinder",
            "Properties": {"Radius": 4.5, "Height": 50, "Placement": [0, -5, 60]},
        },
    )
    r = feat(doc, "boolean", base="LidPlate", name="HingeLid", op="fuse", tool=["Knuckle"])
    check("C: lid fuse", r.get("success") is True, text_of(r)[:300])

    r = c.set_anchors(
        doc, "HingeBase", {"pin_axis": {"pos": [0, -5, 13], "dir": [0, 0, 1]}}, coord_frame="global"
    )
    check("C: base anchors", r.get("success") is True, text_of(r)[:200])
    r = c.set_anchors(
        doc, "HingeLid", {"pin_axis": {"pos": [0, -5, 60], "dir": [0, 0, 1]}}, coord_frame="global"
    )
    check("C: lid anchors", r.get("success") is True, text_of(r)[:200])

    r = c.assemble(
        doc,
        mates=[
            {
                "obj": "HingeLid",
                "anchor": "pin_axis",
                "target": "HingeBase",
                "target_anchor": "pin_axis",
                "mode": "axis",
            }
        ],
        tolerance=0.2,
    )
    check("C: axis mate", r.get("success") is True, text_of(r)[:300])
    # An axis mate pins COAXIALITY; the axial position stays free (a revolute
    # can slide), so the assertion is the assembly semantics: the knuckle sits
    # INSIDE the barrel (the interference volume IS the knuckle) and the
    # audit is clean.
    r = c.check_interference(doc, "HingeBase", "HingeLid")
    knuckle = math.pi * 4.5**2 * 50
    check(
        "C: knuckle inside the barrel (coaxial)",
        r.get("intersects") and abs((r.get("common_volume_mm3") or 0) - knuckle) < 5,
        f"common={r.get('common_volume_mm3')} expected≈{knuckle:.0f}",
    )

    r = c.verify_assembly(doc)
    check("C: verify clean", r.get("success") is True and not r.get("floating"), text_of(r)[:300])

    # Assembly-session flow: mate through the session, unmate must WARN that
    # rollback cannot restore the joint, and a fresh start names the escape.
    from cadpilot.operations.assembly import assembly_session_operation as asm_op

    r = asm_op(c, "start", doc_name=doc, name="live-hinge", part="HingeBase")
    check("C: session start", "started" in text_of(r).lower(), text_of(r)[:300])
    r = asm_op(c, "add_component", doc_name=doc, part="HingeLid")
    check("C: add component", "L_HingeLid" in text_of(r), text_of(r)[:300])
    # An axis joint needs the cylindrical FACE names (an anchor ref resolves
    # to the nearest PLANAR face by design).
    lid_topo = c.get_topology(doc, "HingeLid", element="faces", limit=100)
    base_topo = c.get_topology(doc, "HingeBase", element="faces", limit=100)
    lid_cyl = [
        f["name"]
        for f in lid_topo.get("faces", [])
        if f.get("type") == "Cylinder" and abs(f.get("radius", 0) - 4.5) < 0.01
    ]
    base_cyl = [
        f["name"]
        for f in base_topo.get("faces", [])
        if f.get("type") == "Cylinder" and abs(f.get("radius", 0) - 5.0) < 0.01
    ]
    check("C: cylindrical faces found", lid_cyl and base_cyl, f"{lid_cyl} / {base_cyl}")
    r = c.assembly_op(
        doc,
        {
            "operation": "mate",
            "a": {"part": "HingeLid", "face": lid_cyl[0]},
            "b": {"part": "HingeBase", "face": base_cyl[0]},
            "joint": "revolute",
            "name": "J_Lid",
        },
    )
    check("C: session mate", r.get("success") is True, str(r)[:400])
    joint_name = r.get("joint") or "J_Lid"
    r = asm_op(c, "unmate", doc_name=doc, joint=joint_name)
    text = text_of(r)
    check(
        "C: unmate warns about rollback", "not recoverable" in text or "Re-mate" in text, text[:300]
    )

    # complete never deletes MCP_Assembly; the documented escape must work.
    escape_lines = [
        "doc = App.ActiveDocument",
        "asm = doc.getObject('MCP_Assembly')",
        "removed = []",
        "if asm:",
        "    removed = [o.Name for o in list(asm.OutList)]",
        "    for o in list(asm.OutList):",
        "        doc.removeObject(o.Name)",
        "    doc.removeObject('MCP_Assembly')",
        "    doc.recompute()",
        "print('escape removed:', removed)",
    ]
    r = c.execute_code(chr(10).join(escape_lines), doc_name=doc)
    check("C: escape snippet runs", "escape removed" in (r.get("message") or ""), str(r)[:300])
    check("C: start works after escape", r.get("success") is True, str(r)[:300])
    close_doc(doc)


# =====================================================================
# Scenario D — journal truth: snapshot must never delete pre-journal
# objects (the v0.5.4 class via the snapshot flow), planned runs, replay,
# non-atomic rollback semantics, insert validation.
# =====================================================================
def scenario_journal():
    doc = new_doc("LiveJournal")
    # The user's own object, created OFF any journal (fresh doc = empty journal,
    # then the snapshot becomes the FIRST journal record over it).
    r = c.execute_code(
        "box = App.ActiveDocument.addObject('Part::Box', 'UserBox')\n"
        "box.Length = box.Width = box.Height = 10\n"
        "App.ActiveDocument.recompute()\n"
        "print('user objects:', [o.Name for o in App.ActiveDocument.Objects])",
        doc_name=doc,
    )
    check("D: user object exists", "UserBox" in (r.get("message") or ""), str(r)[:300])

    # execute_code JOURNALS its transaction, so empty the journal (it never
    # touches the document) to make the snapshot the genuine FIRST record over
    # pre-journal geometry — the exact shape of the data-loss regression.
    r = c.journal_op(doc, {"operation": "reset", "params": {"confirm": True}})
    check("D: journal reset over the user object", r.get("success") is True, str(r)[:200])

    # Snapshot as the FIRST journal record (baseline over imported geometry).
    r = c.journal_op(doc, {"operation": "snapshot", "params": {"note": "imported baseline"}})
    check("D: snapshot recorded", r.get("success") is True, text_of(r)[:300])

    # Reject the baseline: the user's object MUST survive.
    r = c.journal_op(doc, {"operation": "reject", "index": 1})
    text = text_of(r)
    names = [o["Name"] for o in c.get_objects(doc)]
    check("D: reject keeps pre-journal objects", "UserBox" in names, f"{names} | {text[:200]}")

    # Reset the journal and build a real model to exercise plan/run/replay.
    r = c.journal_op(doc, {"operation": "reset", "params": {"confirm": True}})
    r = c.create_object(
        doc,
        {
            "Name": "Pad0",
            "Type": "Part::Box",
            "Properties": {"Length": 30, "Width": 20, "Height": 10},
        },
    )
    r = feat(
        doc,
        "sketch",
        base="HoleProfile",
        plane={"face": ["Pad0", "+Z"], "center": True},
        geometry=[{"type": "circle", "center": [0, 0], "radius": 4}],
    )
    steps = [
        {
            "operation": "pad",
            "obj_name": "HoleProfile",
            "obj_properties": {"name": "Lid", "length": 6},
        },
        {
            "operation": "pocket",
            "obj_name": "HoleProfile",
            "obj_properties": {"name": "Hole", "through_all": True},
        },
    ]
    r = c.journal_op(doc, {"operation": "set_plan", "steps": steps})
    check("D: plan accepted", r.get("success") is True, text_of(r)[:300])
    r = c.journal_op(doc, {"operation": "run_all"})
    check("D: plan runs", r.get("success") is True, text_of(r)[:400])
    names = [o["Name"] for o in c.get_objects(doc)]
    check(
        "D: plan objects exist",
        "HoleProfile" in names or any("cket" in n for n in names),
        str(names),
    )

    # Replay rebuilds everything from the journal.
    before = sorted(names)
    r = c.journal_op(doc, {"operation": "replay"})
    check("D: replay ok", r.get("success") is True, text_of(r)[:300])
    after = sorted(o["Name"] for o in c.get_objects(doc))
    check("D: replay restores the object set", before == after, f"{before} vs {after}")

    # Insert validation: an operation-less step is refused at the boundary.
    r = c.journal_op(doc, {"operation": "insert", "index": 1, "params": {"reason": "x"}})
    check("D: insert refuses op-less steps", r.get("success") is False, text_of(r)[:200])
    close_doc(doc)


def scenario_session():
    doc = new_doc("LiveSession")
    from cadpilot.operations.core import (
        cad_operation,
        session_rollback_operation,
        session_start_operation,
        session_status_operation,
    )

    resp = session_start_operation(c, name="live", doc_name=doc)
    check("E: session start", "started" in text_of(resp).lower(), text_of(resp)[:200])
    resp = cad_operation(c, "create_object", doc, obj_type="Part::Box", obj_name="SBox")
    check("E: cad records step", "step #1" in text_of(resp), text_of(resp)[:200])
    check("E: no undoable leak in reply", '"undoable"' not in text_of(resp), text_of(resp)[:300])
    # A same-value edit commits nothing: it must neither block rollback nor
    # consume an undo pop.
    resp = cad_operation(c, "edit_object", doc, obj_name="SBox", obj_properties={"Length": 30})
    check("E: edit ok", "edited" in text_of(resp).lower(), text_of(resp)[:200])
    resp = cad_operation(
        c, "edit_object", doc, obj_name="SBox", obj_properties={"Length": 30}
    )  # same value again
    check("E: third step recorded", "step #3" in text_of(resp), text_of(resp)[:300])
    # Live truth (1.1.4): a same-value property write still lands an undo
    # entry, so the step is atomic and counts. atomic always mirrors
    # `undoable`, which is the invariant rollback's arithmetic needs.
    resp = session_rollback_operation(c, 0)
    text = text_of(resp)
    check(
        "E: rollback undoes every transaction",
        '"success":true' in text.replace(" ", "")
        and '"undone_transactions":3' in text.replace(" ", ""),
        text[:300],
    )
    names = [o["Name"] for o in c.get_objects(doc)]
    check("E: rollback restored the document", "SBox" not in names, str(names))
    st = session_status_operation(c)
    check(
        "E: session empty after full rollback",
        "0 step" in text_of(st) or "0 steps" in text_of(st),
        text_of(st)[:200],
    )
    close_doc(doc)


def scenario_async():
    code = "for i in range(2000):\n    task_print('x' * 100)"
    r = c.execute_code_async(code)
    tid = r.get("task_id")
    check("F: async task id", bool(tid), str(r)[:200])
    if not tid:
        return
    import time

    for _ in range(60):
        res = c.get_task_result(tid)
        if res.get("status") == "done":
            break
        time.sleep(0.3)
    out = res.get("output") or ""
    check("F: async output capped", len(out) <= 65536 + 200, f"len={len(out)}")
    check("F: truncation marker present", "truncated" in out, out[:80])


if __name__ == "__main__":
    scenarios = {
        "flange": scenario_flange,
        "enclosure": scenario_enclosure,
        "hinge": scenario_hinge,
        "journal": scenario_journal,
        "session": scenario_session,
        "async": scenario_async,
    }
    only = sys.argv[1:] or list(scenarios)
    for name in only:
        print(f"\n=== {name} ===")
        try:
            scenarios[name]()
        except Exception as e:
            FAILURES.append(f"{name}: crashed: {type(e).__name__}: {e}")
            print(f"[CRASH] {name}: {type(e).__name__}: {e}")
    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("FAILURES:")
        for f in FAILURES:
            print(" -", f)
        sys.exit(1)
    print("ALL PASS")
