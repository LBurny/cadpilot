"""End-to-end live verification for the sketcher/PartDesign feature ops.

Builds a parametric bracket: variables -> constrained sketch -> pad ->
face sketch -> pocket -> fillet, then verifies the expression chain by
changing a variable and re-measuring, and finally exercises the
under-constrained / conflicting-constraint paths and undo.

Run: uv run --no-sync python scripts/live_sketch_verify.py
"""

import math
import sys

from cadpilot.freecad_client import FreeCADConnection

c = FreeCADConnection()
FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(f"{name}: {detail}")


def feat(op, name=None, **props):
    spec = {"type": op, "base": name, **props}
    return c.create_feature("LiveTest", spec)


# --- setup ----------------------------------------------------------------------
docs = c.list_documents()
if "LiveTest" in docs:
    c.execute_code("import FreeCAD; FreeCAD.closeDocument('LiveTest')")
res = c.create_document("LiveTest")
check("create_document", res.get("success"), str(res))

# --- variables ------------------------------------------------------------------
res = feat(
    "variables",
    "Spreadsheet",
    cells={
        "A1": ["width", 60],
        "A2": ["height", 30],
        "A3": ["thick", 8],
        "A4": ["hole_r", 5],
    },
)
check("variables", res.get("success"), str(res))

# --- fully constrained profile sketch --------------------------------------------
rect_geo = [
    {"type": "line", "from": [0, 0], "to": [60, 0]},
    {"type": "line", "from": [60, 0], "to": [60, 30]},
    {"type": "line", "from": [60, 30], "to": [0, 30]},
    {"type": "line", "from": [0, 30], "to": [0, 0]},
]
rect_cons = [
    {"type": "coincident", "items": [[0, "end"], [1, "start"]]},
    {"type": "coincident", "items": [[1, "end"], [2, "start"]]},
    {"type": "coincident", "items": [[2, "end"], [3, "start"]]},
    {"type": "coincident", "items": [[3, "end"], [0, "start"]]},
    {"type": "horizontal", "items": [[0]]},
    {"type": "horizontal", "items": [[2]]},
    {"type": "vertical", "items": [[1]]},
    {"type": "vertical", "items": [[3]]},
    {"type": "coincident", "items": [[0, "start"], [-1, "center"]]},  # origin anchor
    {"type": "distance_x", "items": [[0, "start"], [0, "end"]], "value": "=Spreadsheet.width"},
    {"type": "distance_y", "items": [[1, "start"], [1, "end"]], "value": "=Spreadsheet.height"},
]
res = feat("sketch", "Profile", plane="XY", geometry=rect_geo, constraints=rect_cons)
check(
    "sketch fully constrained",
    res.get("success") and res.get("fully_constrained") is True,
    str(res),
)
check("sketch reports dof=0", res.get("dof") == 0, str(res))

# --- pad ------------------------------------------------------------------------
res = feat("pad", "Profile", length="=Spreadsheet.thick")
check("pad", res.get("success"), str(res))
pad_name = res.get("object_name", "Pad")

m = c.measure_geometry("LiveTest", pad_name)
expected = 60 * 30 * 8
check(
    "pad volume",
    m.get("success") and abs(m["volume_mm3"] - expected) / expected < 0.01,
    f"volume={m.get('volume_mm3')} expected={expected}",
)

# --- hole sketch on the pad's top face -------------------------------------------
topo = c.get_topology("LiveTest", pad_name, "faces")
top_face = None
for f in topo.get("faces", []):
    n = f.get("normal", [0, 0, 0])
    if abs(n[2] - 1) < 1e-6:
        top_face = f["name"]
check("find top face", top_face is not None, str(topo))

hole_geo = [{"type": "circle", "center": [30, 15], "radius": 5}]
hole_cons = [
    {"type": "distance_x", "items": [[0, "center"]], "value": "=Spreadsheet.width / 2"},
    {"type": "distance_y", "items": [[0, "center"]], "value": "=Spreadsheet.height / 2"},
    {"type": "radius", "items": [[0]], "value": "=Spreadsheet.hole_r"},
]
res = feat(
    "sketch",
    "HoleSketch",
    plane={"face": [pad_name, top_face]},
    geometry=hole_geo,
    constraints=hole_cons,
)
check(
    "face sketch fully constrained",
    res.get("success") and res.get("fully_constrained") is True,
    str(res),
)

res = feat("pocket", "HoleSketch", length="=Spreadsheet.thick")
check("pocket", res.get("success"), str(res))
pocket_name = res.get("object_name", "Pocket")

m = c.measure_geometry("LiveTest", pocket_name)
expected = 60 * 30 * 8 - math.pi * 5**2 * 8
check(
    "pocket volume",
    m.get("success") and abs(m["volume_mm3"] - expected) / expected < 0.01,
    f"volume={m.get('volume_mm3')} expected={expected}",
)

# --- fillet regression (existing op on PartDesign tip) ----------------------------
res = feat("fillet", pocket_name, edges=[0], radius=2)
check("fillet on PartDesign result", res.get("success"), str(res))
fillet_name = res.get("object_name", "Fillet")

# --- expression chain: change width, expect recompute ------------------------------
res = feat("variables", "Spreadsheet", cells={"A1": ["width", 75]})
check("variables update", res.get("success"), str(res))
c.execute_code("import FreeCAD; FreeCAD.getDocument('LiveTest').recompute()")
m = c.measure_geometry("LiveTest", fillet_name)
new_expected = 75 * 30 * 8 - math.pi * 5**2 * 8
# fillet removes a little material; accept a band below the un-filleted volume.
# Volume must also EXCEED the old width-60 value (14400) — proving the
# expression chain recomputed with the new width.
got = m.get("volume_mm3", 0)
check(
    "param change propagates",
    m.get("success") and abs(got - new_expected) / new_expected < 0.05 and got > 60 * 30 * 8,
    f"volume={got} expected≈{new_expected}",
)

# --- under-constrained sketch: success + warning -----------------------------------
res = feat("sketch", "Loose", geometry=[{"type": "circle", "center": [5, 5], "radius": 3}])
check(
    "under-constrained sketch succeeds with warning",
    res.get("success") and res.get("fully_constrained") is False and bool(res.get("warnings")),
    str(res),
)

# --- conflicting sketch: error + no residue ----------------------------------------
before = {o["Name"] for o in c.get_objects("LiveTest")}
conflict_geo = [{"type": "line", "from": [0, 0], "to": [10, 0]}]
conflict_cons = [
    {"type": "distance", "items": [[0]], "value": 10},
    {"type": "distance", "items": [[0]], "value": 20},  # conflicts with the above
]
res = feat("sketch", "Conflict", geometry=conflict_geo, constraints=conflict_cons)
after = {o["Name"] for o in c.get_objects("LiveTest")}
check("conflicting sketch fails", not res.get("success"), str(res))
check("conflict leaves no residue", before == after, f"extra={after - before}")
check(
    "conflict error carries diagnostics",
    not res.get("success")
    and ("onflict" in str(res.get("error", "")) or "rc=" in str(res.get("error", ""))),
    str(res),
)

# --- undo/rollback sanity: undo the last committed op (Loose sketch) ----------------
res = c.undo_transactions("LiveTest", 1)
after_undo = {o["Name"] for o in c.get_objects("LiveTest")}
check("undo removes Loose sketch", res.get("success") and "Loose" not in after_undo, str(res))

# --- summary ------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print(" -", f)
    sys.exit(1)
print("ALL LIVE CHECKS PASSED")
