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
