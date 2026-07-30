"""assembly_state: 装配会话状态机（组件注册表 + 关节序列 + 预计算 undo 回滚）。"""

import pytest

import cadpilot.assembly_state as astate


@pytest.fixture
def asm_home(isolated_home):
    astate.set_current(None)
    yield isolated_home
    astate.set_current(None)


PLACEMENT_ZERO = {
    "Base": {"x": 0, "y": 0, "z": 0},
    "Rotation": {"Axis": {"x": 0, "y": 0, "z": 1}, "Angle": 0},
}


def _empty_undo():
    return {"joints_to_delete": [], "cuts_to_delete": [], "links_restore": {}, "links_repoint": {}}


def test_start_and_record_roundtrip(asm_home):
    s = astate.start_session("Car", "Chassis", "test-asm")
    assert s.ground_part == "Chassis" and s.status == "active"
    assert astate.current_session() is s

    st = astate.record_step(
        s,
        "mate",
        "gearbox->chassis",
        spec_echo={"joint": "fixed"},
        undo={**_empty_undo(), "joints_to_delete": ["J_1"]},
    )
    assert st.step_number == 1

    s2 = astate.load(s.session_id)
    assert s2.ground_part == "Chassis"
    assert s2.steps[0].undo["joints_to_delete"] == ["J_1"]
    assert s2.steps[0].operation == "mate"


def test_plan_rollback_merges_undo_reverse_order(asm_home):
    s = astate.start_session("Car", "Chassis", "t")
    astate.record_step(s, "start", "ground Chassis", {"ground": "Chassis"}, _empty_undo())
    astate.record_step(s, "add_component", "add A", {"part": "A"}, _empty_undo())
    astate.record_step(
        s,
        "mate",
        "J_1",
        {"joint": "fixed"},
        {**_empty_undo(), "joints_to_delete": ["J_1"], "links_restore": {"L_A": PLACEMENT_ZERO}},
    )
    astate.record_step(
        s,
        "mate",
        "J_2+trim",
        {"joint": "fixed"},
        {
            **_empty_undo(),
            "joints_to_delete": ["J_2"],
            "cuts_to_delete": ["TrimCut_1"],
            "links_repoint": {"L_Chas": "Chassis"},
        },
    )

    spec = astate.plan_rollback(s, to_step=2)
    assert spec["operation"] == "rollback_step"
    assert spec["joints_to_delete"] == ["J_2", "J_1"]  # 逆序
    assert spec["cuts_to_delete"] == ["TrimCut_1"]
    assert spec["links_restore"] == {"L_A": PLACEMENT_ZERO}
    assert spec["links_repoint"] == {"L_Chas": "Chassis"}


def test_truncate_after_rollback(asm_home):
    s = astate.start_session("Car", "Chassis", "t")
    astate.record_step(s, "start", "ground", {}, _empty_undo())
    st2 = astate.record_step(s, "add_component", "A", {"part": "A"}, _empty_undo())
    s.components["A"] = {"link": "L_A", "added_step": st2.step_number}
    st3 = astate.record_step(s, "mate", "J_1", {}, _empty_undo())
    s.joints.append(
        {
            "step": st3.step_number,
            "name": "J_1",
            "joint_type": "fixed",
            "a": {},
            "b": {},
            "trim": None,
        }
    )
    st4 = astate.record_step(s, "add_component", "B", {"part": "B"}, _empty_undo())
    s.components["B"] = {"link": "L_B", "added_step": st4.step_number}
    astate.save(s)

    astate.truncate_after_rollback(s, to_step=2)
    assert [st.step_number for st in s.steps] == [1, 2]
    assert s.joints == []
    assert list(s.components) == ["A"]


def test_list_sessions(asm_home):
    astate.start_session("Car", "Chassis", "asm-1")
    astate.start_session("Car", "Chassis", "asm-2")
    sessions = astate.list_sessions()
    assert len(sessions) == 2
    assert {s["name"] for s in sessions} == {"asm-1", "asm-2"}


def test_load_missing_session_returns_none(asm_home):
    assert astate.load("does-not-exist") is None
    assert astate.resume_session("does-not-exist") is None
    assert astate.current_session() is None


def test_load_corrupt_json_returns_none(asm_home):
    s = astate.start_session("Car", "Chassis", "t")
    path = astate.assembly_dir() / f"{s.session_id}.json"
    path.write_text("{ not valid json", encoding="utf-8")
    assert astate.load(s.session_id) is None
    # corrupt entries are skipped, not fatal
    assert astate.list_sessions() == []


def test_load_structurally_invalid_returns_none(asm_home):
    s = astate.start_session("Car", "Chassis", "t")
    path = astate.assembly_dir() / f"{s.session_id}.json"
    path.write_text('{"session_id": "x"}', encoding="utf-8")  # missing fields
    assert astate.load(s.session_id) is None


def test_save_is_atomic_when_dump_fails(asm_home, monkeypatch):
    """A failed save must not truncate/corrupt the previously saved file."""
    s = astate.start_session("Car", "Chassis", "t")
    path = astate.assembly_dir() / f"{s.session_id}.json"
    original = path.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(astate.json, "dump", boom)
    with pytest.raises(OSError):
        astate.save(s)
    assert path.read_bytes() == original
    assert not path.with_suffix(".tmp").exists()
