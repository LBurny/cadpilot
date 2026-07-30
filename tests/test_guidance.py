"""Tests for guidance heuristics."""

from cadpilot.guidance import detect_risks, suggest_next_steps
from cadpilot.session_state import new_session


def _session_with_steps(ops_atomic: list[tuple[str, bool]]):
    sess = new_session("g", "Doc")
    for op, atomic in ops_atomic:
        sess.add_step(op, f"{op} step", objects_after=["Box"], atomic=atomic)
    return sess


def test_empty_doc_suggests_create():
    sess = new_session("g", "Doc")
    suggestions = suggest_next_steps(sess, [])
    assert suggestions[0]["tool"] == "cad"
    assert suggestions[0]["operation"] == "create_object"


def test_non_atomic_step_suggests_cad_and_risk():
    sess = _session_with_steps([("create_object", True), ("execute_code", False)])
    suggestions = suggest_next_steps(sess, ["Box"])
    assert any("execute_code" in s["reason"] for s in suggestions)
    risks = detect_risks(sess, doc_open=True, object_names=["Box"])
    assert any(r["type"] == "non_atomic_steps" for r in risks)


def test_closed_document_risk_short_circuits():
    sess = _session_with_steps([("create_object", True)])
    risks = detect_risks(sess, doc_open=False, object_names=None)
    assert risks[0]["type"] == "document_closed"
    assert len(risks) == 1


def test_state_drift_risk():
    sess = _session_with_steps([("create_object", True)])
    risks = detect_risks(sess, doc_open=True, object_names=["Box", "ExtraFromGui"])
    assert any(r["type"] == "state_drift" for r in risks)


def test_no_drift_when_matching():
    sess = _session_with_steps([("create_object", True)])
    risks = detect_risks(sess, doc_open=True, object_names=["Box"])
    assert not any(r["type"] == "state_drift" for r in risks)


def test_redo_buffer_info_risk():
    sess = _session_with_steps([("create_object", True), ("edit_object", True)])
    sess.truncate_to(1)
    risks = detect_risks(sess, doc_open=True, object_names=["Box"])
    assert any(r["type"] == "redo_available" for r in risks)


def test_long_session_suggests_complete():
    sess = new_session("g", "Doc")
    for _ in range(10):
        sess.add_step("create_object", "s", objects_after=["Box"])
    suggestions = suggest_next_steps(sess, ["Box"])
    assert any(s["tool"] == "session_complete" for s in suggestions)


def test_disconnected_islands_risk():
    sess = new_session("g", "Doc")
    sess.add_step(
        "create_object",
        "create Box",
        result_summary="Object 'Box' created successfully\n⚠ Connectivity: 2 disconnected island(s) not touching the main assembly:\n  - [Spoke_0]",
        objects_after=["Box"],
        atomic=True,
    )
    risks = detect_risks(sess, doc_open=True, object_names=["Box"])
    assert any(r["type"] == "disconnected_islands" for r in risks)


def test_no_islands_risk_when_summary_clean():
    sess = _session_with_steps([("create_object", True)])
    risks = detect_risks(sess, doc_open=True, object_names=["Box"])
    assert not any(r["type"] == "disconnected_islands" for r in risks)


def test_primitive_without_sketch_risk():
    sess = new_session("g", "Doc")
    sess.add_step("create_object", "c", params_summary="Part::Box 'A'", objects_after=["A"])
    sess.add_step(
        "create_object", "c", params_summary="Part::Cylinder 'B'", objects_after=["A", "B"]
    )
    risks = detect_risks(sess, doc_open=True, object_names=["A", "B"])
    assert any(r["type"] == "primitive_without_sketch" for r in risks)


def test_no_primitive_risk_when_sketch_used():
    sess = new_session("g", "Doc")
    sess.add_step("create_object", "c", params_summary="Part::Box 'A'", objects_after=["A"])
    sess.add_step(
        "create_object", "c", params_summary="Part::Cylinder 'B'", objects_after=["A", "B"]
    )
    sess.add_step(
        "sketch", "s", params_summary="on 'None' ['plane']", objects_after=["A", "B", "Sketch"]
    )
    risks = detect_risks(sess, doc_open=True, object_names=["A", "B", "Sketch"])
    assert not any(r["type"] == "primitive_without_sketch" for r in risks)


def test_absolute_placement_risk():
    sess = new_session("g", "Doc")
    sess.add_step(
        "create_object", "c", params_summary="Part::Box 'A' +Placement", objects_after=["A"]
    )
    risks = detect_risks(sess, doc_open=True, object_names=["A"])
    assert any(r["type"] == "absolute_placement" for r in risks)


def test_multi_part_doc_suggests_assembly_mode():
    sess = _session_with_steps([("create_object", True)])
    suggestions = suggest_next_steps(sess, ["A", "B"])
    assert any(s["tool"] == "assembly_session" for s in suggestions)


def test_assembly_suggestion_suppressed_after_assemble(monkeypatch):
    from cadpilot import assembly_state as astate

    monkeypatch.setattr(astate, "current_session", lambda: None)
    sess = _session_with_steps([("create_object", True), ("assemble", True)])
    suggestions = suggest_next_steps(sess, ["A", "B"])
    assert not any(s["tool"] == "assembly_session" for s in suggestions)
