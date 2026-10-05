"""assembly_session 工具的操作实现：规格校验 → RPC → 状态机记录（TDD）。"""

import pytest

import cadpilot.assembly_state as astate
from cadpilot.operations.assembly import assembly_session_operation


@pytest.fixture
def asm_home(isolated_home):
    astate.set_current(None)
    yield isolated_home
    astate.set_current(None)


def _fake_assembly_result(method_calls):
    """根据 spec.operation 返回像样的结果。"""
    spec = method_calls[1]
    op = spec["operation"]
    if op == "start":
        return {
            "assembly": "MCP_Assembly",
            "joint_group": "Joints",
            "ground_link": f"L_{spec['ground']}",
            "ground_joint": "GroundedJoint",
        }
    if op == "add_component":
        return {
            "link": f"L_{spec['part']}",
            "placement": {
                "Base": {"x": 0, "y": 0, "z": 0},
                "Rotation": {"Axis": {"x": 0, "y": 0, "z": 1}, "Angle": 0},
            },
        }
    if op == "mate":
        res = {
            "joint": "J_MCP",
            "residual_mm": 0.0,
            "residual_deg": 0.0,
            "moved_link": f"L_{spec['a']['part']}",
            "pre_placement": {
                "Base": {"x": 9, "y": 9, "z": 9},
                "Rotation": {"Axis": {"x": 0, "y": 0, "z": 1}, "Angle": 0},
            },
        }
        if "trim" in spec:
            loser = spec["b"]["part"] if spec["trim"]["winner"] == "inserted" else spec["a"]["part"]
            res["trim"] = {"cut": f"TrimCut_{loser}", "overlap_mm3": 1570.8}
        return res
    if op == "solve":
        return {"joints": [{"name": "J_MCP", "residual_mm": 0.0, "residual_deg": 0.0}]}
    return {"ok": True}


@pytest.fixture
def asm_conn(fake_freecad, monkeypatch):
    def assembly_op(doc_name, spec):
        fake_freecad._record("assembly_op", doc_name, spec)
        return _fake_assembly_result((doc_name, spec))

    monkeypatch.setattr(fake_freecad, "assembly_op", assembly_op, raising=False)
    return fake_freecad


def _specs(conn):
    return [c[1][1] for c in conn.calls if c[0] == "assembly_op"]


def test_start_sends_rpc_and_creates_session(asm_conn, asm_home):
    r = assembly_session_operation(asm_conn, "start", doc_name="Car", name="t", part="Chassis")
    spec = _specs(asm_conn)[-1]
    assert spec["operation"] == "start" and spec["ground"] == "Chassis"
    s = astate.current_session()
    assert s is not None and s.ground_part == "Chassis"
    # ground 也注册为组件（Link 包装在 addon 侧完成）
    assert s.components["Chassis"]["link"] == "L_Chassis"
    assert "started" in r[0].text.lower()


def test_mate_requires_active_session(asm_conn, asm_home):
    r = assembly_session_operation(
        asm_conn, "mate", a={"part": "A", "face": "Face1"}, b={"part": "B", "face": "Face1"}
    )
    assert "no active" in r[0].text.lower()


def test_mate_validates_refs_and_joint_type(asm_conn, asm_home):
    assembly_session_operation(asm_conn, "start", doc_name="Car", part="Chassis")
    r = assembly_session_operation(
        asm_conn,
        "mate",
        joint_type="welded",
        a={"part": "Chassis", "face": "Face1"},
        b={"part": "Chassis", "face": "Face2"},
    )
    assert "joint_type" in r[0].text
    r = assembly_session_operation(
        asm_conn,
        "mate",
        a={"part": "Chassis"},  # 缺 face/anchor/point
        b={"part": "Chassis", "face": "Face2"},
    )
    assert "exactly one" in r[0].text
    r = assembly_session_operation(
        asm_conn,
        "mate",
        a={"part": "Ghost", "face": "Face1"},
        b={"part": "Chassis", "face": "Face2"},
    )
    assert "not a component" in r[0].text


def test_mate_records_step_joint_and_undo(asm_conn, asm_home):
    assembly_session_operation(asm_conn, "start", doc_name="Car", part="Chassis")
    assembly_session_operation(asm_conn, "add_component", part="Gear")
    r = assembly_session_operation(
        asm_conn,
        "mate",
        a={"part": "Gear", "anchor": "flange"},
        b={"part": "Chassis", "face": "Face2"},
    )
    s = astate.current_session()
    assert s.joints[0]["name"] == "J_MCP" and s.joints[0]["step"] == 3
    undo = s.steps[-1].undo
    assert undo["joints_to_delete"] == ["J_MCP"]
    # 移动侧（a=Gear）的装配前位姿快照进了 undo
    assert undo["links_restore"]["L_Gear"]["Base"]["x"] == 9
    assert "0.0" in r[0].text


def test_mate_restores_every_link_the_solver_actually_moved(asm_conn, asm_home, monkeypatch):
    """The solver picks the side it moves (usually b's), and a mate can shift
    several links in one solve. The old record named ref_a's link only, so a
    rollback restored a part that had never moved and left the moved one
    displaced (live: a hinge lid stayed put after rollback)."""
    assembly_session_operation(asm_conn, "start", doc_name="Car", part="Chassis")
    assembly_session_operation(asm_conn, "add_component", part="Lid")

    def moved_mate(doc_name, spec):
        asm_conn._record("assembly_op", doc_name, spec)
        return {
            "joint": "J_MCP",
            "residual_mm": 0.0,
            "residual_deg": 0.0,
            "moved_link": "L_Lid",
            "pre_placement": {
                "Base": {"x": 0, "y": 0, "z": 150},
                "Rotation": {"Axis": {"x": 0, "y": 0, "z": 1}, "Angle": 0},
            },
            "moved_links": {
                "L_Lid": {
                    "pre": {
                        "Base": {"x": 0, "y": 0, "z": 150},
                        "Rotation": {"Axis": {"x": 0, "y": 0, "z": 1}, "Angle": 0},
                    },
                    "to": {
                        "Base": {"x": 0, "y": 0, "z": 20},
                        "Rotation": {"Axis": {"x": 0, "y": 0, "z": 1}, "Angle": 0},
                    },
                },
                "L_Chassis": {
                    "pre": {
                        "Base": {"x": 1, "y": 0, "z": 0},
                        "Rotation": {"Axis": {"x": 0, "y": 0, "z": 1}, "Angle": 0},
                    },
                    "to": {
                        "Base": {"x": 2, "y": 0, "z": 0},
                        "Rotation": {"Axis": {"x": 0, "y": 0, "z": 1}, "Angle": 0},
                    },
                },
            },
        }

    monkeypatch.setattr(asm_conn, "assembly_op", moved_mate, raising=False)
    assembly_session_operation(
        asm_conn,
        "mate",
        a={"part": "Chassis", "face": "Face1"},
        b={"part": "Lid", "face": "Face1"},
    )
    undo = astate.current_session().steps[-1].undo
    assert set(undo["links_restore"]) == {"L_Lid", "L_Chassis"}
    assert undo["links_restore"]["L_Lid"]["Base"]["z"] == 150
    assert undo["links_restore"]["L_Chassis"]["Base"]["x"] == 1


def test_mate_that_moved_nothing_records_no_restore(asm_conn, asm_home, monkeypatch):
    """The addon now reports moved_link=None when the solver satisfied the mate
    without displacement; the recorder must then store no placement snapshot
    (restoring a part that never moved would teleport it later)."""
    assembly_session_operation(asm_conn, "start", doc_name="Car", part="Chassis")
    assembly_session_operation(asm_conn, "add_component", part="Gear")

    def no_move(doc_name, spec):
        asm_conn._record("assembly_op", doc_name, spec)
        return {
            "joint": "J_MCP",
            "residual_mm": 0.0,
            "residual_deg": 0.0,
            "moved_link": None,
            "moved_links": {},
            "warnings": ["the mate was satisfied without moving any component"],
        }

    monkeypatch.setattr(asm_conn, "assembly_op", no_move, raising=False)
    r = assembly_session_operation(
        asm_conn,
        "mate",
        a={"part": "Gear", "face": "Face1"},
        b={"part": "Chassis", "face": "Face1"},
    )
    undo = astate.current_session().steps[-1].undo
    assert undo["links_restore"] == {}
    assert undo["joints_to_delete"] == ["J_MCP"]
    assert "without moving any component" in r[0].text


def test_start_undo_tears_the_assembly_down_on_rollback_across_it(asm_conn, asm_home):
    """to_step=0 used to leave MCP_Assembly, its ground joint and the ground
    link behind — the start step carried an empty undo."""
    assembly_session_operation(asm_conn, "start", doc_name="Car", part="Chassis")
    assembly_session_operation(asm_conn, "add_component", part="Lid")
    s = astate.current_session()
    start_undo = s.steps[0].undo
    assert start_undo["remove_assembly"] == "MCP_Assembly"
    assert start_undo["remove_links"] == ["L_Chassis"]
    assert start_undo["joints_to_delete"] == ["GroundedJoint"]
    plan = astate.plan_rollback(s, 0)
    assert plan["remove_assembly"] == "MCP_Assembly"
    assert "L_Chassis" in plan["remove_links"]
    # Rolling back to step 1 (keeping start) must NOT tear the assembly down.
    assert astate.plan_rollback(s, 1)["remove_assembly"] is None


def test_mate_with_trim_undo_covers_cut_and_repoint(asm_conn, asm_home):
    assembly_session_operation(asm_conn, "start", doc_name="Car", part="Chassis")
    assembly_session_operation(asm_conn, "add_component", part="Pin")
    assembly_session_operation(
        asm_conn,
        "mate",
        a={"part": "Pin", "face": "Face1"},
        b={"part": "Chassis", "face": "Face6"},
        trim={"winner": "inserted"},
    )
    undo = astate.current_session().steps[-1].undo
    assert undo["cuts_to_delete"] == ["TrimCut_Chassis"]
    assert undo["links_repoint"] == {"L_Chassis": "Chassis"}


def test_rollback_sends_merged_spec_and_truncates(asm_conn, asm_home):
    assembly_session_operation(asm_conn, "start", doc_name="Car", part="Chassis")
    assembly_session_operation(asm_conn, "add_component", part="Gear")
    assembly_session_operation(
        asm_conn,
        "mate",
        a={"part": "Gear", "face": "Face1"},
        b={"part": "Chassis", "face": "Face2"},
    )
    r = assembly_session_operation(asm_conn, "rollback", to_step=2)
    spec = _specs(asm_conn)[-1]
    assert spec["operation"] == "rollback_step"
    assert spec["joints_to_delete"] == ["J_MCP"]
    assert "L_Gear" in spec["links_restore"]
    s = astate.current_session()
    assert [st.step_number for st in s.steps] == [1, 2]
    assert s.joints == []
    assert '"rolled_back_to_step":2' in r[0].text.replace(" ", "")


def test_complete_closes_session(asm_conn, asm_home):
    assembly_session_operation(asm_conn, "start", doc_name="Car", part="Chassis")
    assembly_session_operation(asm_conn, "complete")
    assert astate.current_session() is None


def test_server_tool_routes_to_operation(asm_conn, asm_home, monkeypatch):
    from cadpilot import server

    monkeypatch.setattr(server, "get_freecad_connection", lambda: asm_conn)
    r = server.assembly_session(None, operation="start", doc_name="Car", part="Chassis")
    assert asm_conn.calls[-1][0] == "assembly_op"
    assert astate.current_session() is not None
    assert "started" in r[0].text


def _fail_rpc(conn, monkeypatch, error="solver exploded"):
    monkeypatch.setattr(
        conn,
        "assembly_op",
        lambda d, s: {"success": False, "error": error},
        raising=False,
    )


def test_start_rpc_failure_creates_no_session(asm_conn, asm_home, monkeypatch):
    _fail_rpc(asm_conn, monkeypatch)
    r = assembly_session_operation(asm_conn, "start", doc_name="Car", part="Chassis")
    assert "solver exploded" in r[0].text
    assert astate.current_session() is None


def test_add_component_rpc_failure_records_nothing(asm_conn, asm_home, monkeypatch):
    assembly_session_operation(asm_conn, "start", doc_name="Car", part="Chassis")
    _fail_rpc(asm_conn, monkeypatch)
    r = assembly_session_operation(asm_conn, "add_component", part="Gear")
    assert "solver exploded" in r[0].text
    s = astate.current_session()
    assert "Gear" not in s.components
    assert len(s.steps) == 1  # only the start step


def test_mate_rpc_failure_records_nothing(asm_conn, asm_home, monkeypatch):
    assembly_session_operation(asm_conn, "start", doc_name="Car", part="Chassis")
    assembly_session_operation(asm_conn, "add_component", part="Gear")
    _fail_rpc(asm_conn, monkeypatch)
    r = assembly_session_operation(
        asm_conn,
        "mate",
        a={"part": "Gear", "face": "Face1"},
        b={"part": "Chassis", "face": "Face2"},
    )
    assert "solver exploded" in r[0].text
    s = astate.current_session()
    assert s.joints == []
    assert len(s.steps) == 2  # start + add_component only


def test_mate_trim_result_without_user_trim_does_not_crash(asm_conn, asm_home, monkeypatch):
    """addon 返回 trim 数据但用户没传 trim 时不得崩溃，也不记录裁剪 undo。"""
    assembly_session_operation(asm_conn, "start", doc_name="Car", part="Chassis")
    assembly_session_operation(asm_conn, "add_component", part="Gear")
    monkeypatch.setattr(
        asm_conn,
        "assembly_op",
        lambda d, s: {"joint": "J_X", "trim": {"cut": "TrimCut_Gear"}},
        raising=False,
    )
    r = assembly_session_operation(
        asm_conn,
        "mate",
        a={"part": "Gear", "face": "Face1"},
        b={"part": "Chassis", "face": "Face2"},
    )
    assert "J_X" in r[0].text
    undo = astate.current_session().steps[-1].undo
    assert undo["cuts_to_delete"] == []
    assert undo["links_repoint"] == {}


# --- addon-side regression guards (joint_ops.py; AST, no FreeCAD import) ----

import ast  # noqa: E402
from pathlib import Path  # noqa: E402

_JOINT_OPS = ast.parse(
    (
        Path(__file__).resolve().parents[1] / "addon" / "CADPilot" / "rpc_server" / "joint_ops.py"
    ).read_text(encoding="utf-8")
)


def _func(tree, name):
    return next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )


def test_anchor_ref_unpacks_the_four_tuple_and_surfaces_errors():
    """assembly_ops._resolve_anchor returns (pos, dir, source, error); the mate
    ref resolver used to unpack 2 values, so EVERY anchor-based mate crashed
    with 'too many values to unpack' before any geometry was touched."""
    body = ast.get_source_segment(
        (
            Path(__file__).resolve().parents[1]
            / "addon"
            / "CADPilot"
            / "rpc_server"
            / "joint_ops.py"
        ).read_text(encoding="utf-8"),
        _func(_JOINT_OPS, "_resolve_ref"),
    )
    assert "_resolve_anchor" in body
    assert "_dir, _src, aerr" in body or body.count("aerr") >= 2, (
        "unpack the error slot and propagate it"
    )


def test_anchor_ref_maps_the_anchor_into_the_link_frame():
    """After add_component the link owns the placement and the base part sits
    at identity — the anchor resolves in the base frame, so probing
    link.Shape without mapping through link.Placement probes the wrong place."""
    body = ast.get_source_segment(
        (
            Path(__file__).resolve().parents[1]
            / "addon"
            / "CADPilot"
            / "rpc_server"
            / "joint_ops.py"
        ).read_text(encoding="utf-8"),
        _func(_JOINT_OPS, "_resolve_ref"),
    )
    assert "multVec" in body, "anchor positions must be mapped through link.Placement"


def test_rollback_restores_placements_before_removing_links():
    """remove_links hands the link's CURRENT placement back to the part;
    restoring placements only afterwards silently leaks the post-mate
    placement into the base part (live-caught: rollback left the peg at its
    mated corner instead of its pre-mate position)."""
    fn = _func(_JOINT_OPS, "_op_rollback_step")
    lines = {"links_restore": [], "remove_links": []}
    for n in ast.walk(fn):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "get"
            and n.args
            and isinstance(n.args[0], ast.Constant)
            and n.args[0].value in lines
        ):
            lines[n.args[0].value].append(n.lineno)
    assert lines["links_restore"] and lines["remove_links"], "both loops must exist"
    assert min(lines["links_restore"]) < min(lines["remove_links"]), (
        "links_restore must run before remove_links"
    )


def test_mate_reports_landing_and_where_the_jcs_landed():
    """`landing` tells which sub-elements the refs resolved to and
    `landing_points` where the JCS actually landed — the number to check a
    mate against, since residual 0 only says the two frames agree, not that
    they landed where the caller meant (live: a fixed mate of two concentric
    circular faces came out 44 mm off axis at residual 0)."""
    fn = _func(_JOINT_OPS, "_op_mate")
    src = ast.get_source_segment(
        (
            Path(__file__).resolve().parents[1]
            / "addon"
            / "CADPilot"
            / "rpc_server"
            / "joint_ops.py"
        ).read_text(encoding="utf-8"),
        fn,
    )
    assert '"landing"' in src
    assert '"landing_points"' in src
    assert '"warnings"' in src
    assert "_landing_warnings" in src


def test_landing_warnings_measure_distance_to_each_ref_kinds_intent():
    """A plain face ref lands on the face CENTER by construction (the
    face-name marker), so only hint/anchor/point refs can snap somewhere else:
    they pick the closest selectable point, which is arbitrary on a symmetric
    face. The warning must compare the landing point with the actual intent of
    the ref kind: the point_on_face hint, the anchor's position, the point
    itself."""
    fn = _func(_JOINT_OPS, "_landing_warnings")
    src = ast.get_source_segment(
        (
            Path(__file__).resolve().parents[1]
            / "addon"
            / "CADPilot"
            / "rpc_server"
            / "joint_ops.py"
        ).read_text(encoding="utf-8"),
        fn,
    )
    assert "_landing_point" in src, "measure where the ref actually lands"
    assert "point_on_face" in src, "a plain face ref lands on the center — skip it"
    assert "_resolve_anchor" in src, "anchor refs measure against the anchor position"
    assert "multVec" in src, "anchor positions are local — map into the link frame"
    assert 'r["point"]' in src, "point refs measure against the point itself"


def test_face_refs_land_on_the_face_center_not_a_seam_vertex():
    """UtilsAssembly.findPlacement lands by the SECOND element's type: a
    Vertex at that vertex, a circular edge at its CENTER, the face's own name
    at the face center. Always appending the nearest vertex mis-landed every
    circular face (its only vertex is OCC's seam): concentric holes came out
    R-r off axis. The resolver must use the GUI's own [sub, sub] marker for a
    plain face ref and offer circular-edge centers as click candidates."""
    src = ast.unparse(_func(_JOINT_OPS, "_face_landing"))
    assert "return face_name" in src, "the face-name marker lands on the face center"
    assert "GeomCircle" in src and "Curve.Location" in src, (
        "a circular boundary edge lands at its center"
    )
    assert "_shape_edge_name" in src, "face-local edges must be named shape-globally"
    assert "_shape_vertex_name" in src
    landing = ast.unparse(_func(_JOINT_OPS, "_landing_point"))
    assert "CenterOfGravity" in landing and "Curve.Location" in landing
    resolve = ast.unparse(_func(_JOINT_OPS, "_resolve_ref"))
    assert "_face_landing" in resolve


def test_assembly_ops_refuse_another_documents_session(asm_conn, asm_home):
    """Same global-slot hazard as session(): assembly ops took the active
    session's doc and ignored the passed doc_name, so an agent's rollback
    dismantled the OTHER agent's assembly (live: rollback(doc_name=R4G_Asm)
    returned R4G_Asm2's objects while R4G_Asm never moved)."""
    r = assembly_session_operation(asm_conn, "start", doc_name="Car", part="Chassis")
    assert '"success"' in r[0].text or "started" in r[0].text
    r = assembly_session_operation(asm_conn, "status", doc_name="Other")
    assert "tracks document 'Car'" in r[0].text, r[0].text
    # The session's own document (and the no-doc_name legacy form) still work.
    ok = assembly_session_operation(asm_conn, "status", doc_name="Car")
    assert "tracks document" not in ok[0].text
    legacy = assembly_session_operation(asm_conn, "status")
    assert "tracks document" not in legacy[0].text


def test_start_reports_replacing_another_documents_session(asm_conn, asm_home):
    assembly_session_operation(asm_conn, "start", doc_name="DocA", part="A")
    r = assembly_session_operation(asm_conn, "start", doc_name="DocB", part="B")
    blob = "".join(getattr(c, "text", str(c)) for c in r)
    assert "replaced the active assembly session" in blob, blob
