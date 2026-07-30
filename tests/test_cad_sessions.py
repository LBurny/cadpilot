"""Tests for the unified cad() dispatcher and modeling-session operations."""

import json

from cadpilot.operations import (
    cad_operation,
    execute_code_operation,
    inspect_freecad_operation,
    recall_patterns_operation,
    save_pattern_operation,
    session_add_note_operation,
    session_complete_operation,
    session_get_steps_operation,
    session_list_operation,
    session_pause_operation,
    session_redo_operation,
    session_resume_operation,
    session_rollback_operation,
    session_start_operation,
    session_status_operation,
)
from cadpilot.session_state import get_current_session


def _text(resp) -> str:
    return resp[0].text


def _json(resp) -> dict:
    return json.loads(resp[0].text)


def _start_session(fake_freecad, doc="Doc"):
    fake_freecad.documents = [doc]
    resp = session_start_operation(fake_freecad, doc, "test session")
    assert _json(resp)["success"] is True
    return get_current_session()


# --- cad dispatch -------------------------------------------------------------


def test_cad_create_records_step_with_fingerprint(fake_freecad, isolated_home):
    sess = _start_session(fake_freecad)
    fake_freecad.objects_by_doc["Doc"] = ["Box"]
    resp = cad_operation(
        fake_freecad,
        False,
        "create_object",
        "Doc",
        obj_type="Part::Box",
        obj_name="Box",
        description="base box",
    )
    assert "step #1" in _text(resp)
    assert sess.step_count == 1
    step = sess.steps[0]
    assert step.operation == "create_object"
    assert step.description == "base box"
    assert step.objects_after == ["Box"]
    assert step.atomic is True  # addon reported a committed transaction


def test_cad_marks_non_atomic_when_transaction_missing(fake_freecad, isolated_home):
    sess = _start_session(fake_freecad)
    fake_freecad.result_overrides["create_object"] = {
        "success": True,
        "object_name": "Box",
        "transaction": False,
        "objects": ["Box"],
    }
    cad_operation(fake_freecad, False, "create_object", "Doc", obj_type="Part::Box", obj_name="Box")
    assert sess.steps[0].atomic is False


def test_cad_failure_not_recorded(fake_freecad, isolated_home):
    sess = _start_session(fake_freecad)
    fake_freecad.result_overrides["create_object"] = {"success": False, "error": "boom"}
    resp = cad_operation(
        fake_freecad, False, "create_object", "Doc", obj_type="Part::Box", obj_name="Box"
    )
    assert "boom" in _text(resp)
    assert sess.step_count == 0


def test_cad_other_document_not_recorded(fake_freecad, isolated_home):
    sess = _start_session(fake_freecad, doc="Doc")
    resp = cad_operation(
        fake_freecad, False, "create_object", "OtherDoc", obj_type="Part::Box", obj_name="Box"
    )
    assert "not recorded" in _text(resp)
    assert sess.step_count == 0


def test_cad_no_session_runs_untracked(fake_freecad, isolated_home):
    resp = cad_operation(
        fake_freecad, False, "create_object", "Doc", obj_type="Part::Box", obj_name="Box"
    )
    assert "created successfully" in _text(resp)
    assert "step #" not in _text(resp)


def test_cad_edit_and_delete_dispatch(fake_freecad, isolated_home):
    _start_session(fake_freecad)
    cad_operation(
        fake_freecad, False, "edit_object", "Doc", obj_name="Box", obj_properties={"Length": 5}
    )
    cad_operation(fake_freecad, False, "delete_object", "Doc", obj_name="Box")
    methods = fake_freecad.called_methods()
    assert "edit_object" in methods and "delete_object" in methods


def test_cad_batch_partial_success_still_recorded(fake_freecad, isolated_home):
    """The addon commits a batch when ≥1 op succeeded, so the step log must
    record it even though the top-level success flag is False."""
    sess = _start_session(fake_freecad)
    fake_freecad.result_overrides["execute_operations"] = {
        "success": False,
        "results": [
            {"success": True, "action": "create_object"},
            {"success": False, "action": "edit_object", "error": "bad"},
        ],
        "transaction": True,
        "objects": ["Box"],
    }
    ops = [{"action": "create_object"}, {"action": "edit_object"}]
    resp = cad_operation(fake_freecad, False, "batch", "Doc", ops=ops)
    data = _json(resp)
    assert "1/2" in data["summary"]
    assert sess.step_count == 1


def test_cad_batch_all_failed_not_recorded(fake_freecad, isolated_home):
    sess = _start_session(fake_freecad)
    fake_freecad.result_overrides["execute_operations"] = {
        "success": False,
        "results": [{"success": False, "action": "create_object", "error": "x"}],
        "transaction": False,
    }
    cad_operation(fake_freecad, False, "batch", "Doc", ops=[{"action": "create_object"}])
    assert sess.step_count == 0


def test_cad_unknown_operation(fake_freecad, isolated_home):
    resp = cad_operation(fake_freecad, False, "fly_to_moon", "Doc")
    assert "Unknown cad operation" in _text(resp)


def test_cad_requires_params(fake_freecad, isolated_home):
    resp = cad_operation(fake_freecad, False, "create_object", "Doc")
    assert "requires obj_type" in _text(resp)


def test_cad_screenshot_only_when_requested(fake_freecad, isolated_home):
    resp = cad_operation(
        fake_freecad, True, "create_object", "Doc", obj_type="Part::Box", obj_name="Box"
    )
    _, args, _ = fake_freecad.calls[0]
    assert args[2] == {"view_name": "Isometric"}
    assert any(c.type == "image" for c in resp)


# --- execute_code non-atomic step ----------------------------------------------


def test_execute_code_records_non_atomic_step(fake_freecad, isolated_home):
    sess = _start_session(fake_freecad)
    resp = execute_code_operation(fake_freecad, False, "print(1)")
    assert "non-atomic step #1" in _text(resp)
    assert sess.steps[0].atomic is False


# --- rollback / redo -------------------------------------------------------------


def _three_step_session(fake_freecad):
    sess = _start_session(fake_freecad)
    for i, name in enumerate(["A", "B", "C"]):
        sess.add_step("create_object", f"create {name}", objects_after=["A", "B", "C"][: i + 1])
    return sess


def test_rollback_undoes_and_truncates(fake_freecad, isolated_home):
    sess = _three_step_session(fake_freecad)
    fake_freecad.objects_by_doc["Doc"] = ["A"]  # post-rollback fingerprint
    resp = session_rollback_operation(fake_freecad, 1)
    data = _json(resp)
    assert data["success"] is True
    _, args, _ = fake_freecad.calls[-1]
    assert args == ("Doc", 2)  # undo 2 transactions
    assert data["removed_steps"] == [2, 3]
    assert sess.step_count == 1
    assert len(sess.redo_buffer) == 2
    assert data["state_matches_log"] is True


def test_rollback_reports_fingerprint_drift(fake_freecad, isolated_home):
    _three_step_session(fake_freecad)
    fake_freecad.objects_by_doc["Doc"] = ["A", "GuiAdded"]
    resp = session_rollback_operation(fake_freecad, 1)
    data = _json(resp)
    assert data["state_matches_log"] is False
    assert data["warnings"]


def test_rollback_blocked_by_non_atomic_step(fake_freecad, isolated_home):
    sess = _start_session(fake_freecad)
    sess.add_step("create_object", "a", objects_after=["A"])
    sess.add_step("execute_code", "b", atomic=False)
    resp = session_rollback_operation(fake_freecad, 0)
    assert "Cannot roll back past step(s) [2]" in _text(resp)
    assert sess.step_count == 2  # unchanged

    resp = session_rollback_operation(fake_freecad, 0, force=True)
    assert _json(resp)["success"] is True
    assert sess.step_count == 0


def test_rollback_partial_undo_truncates_to_match(fake_freecad, isolated_home):
    sess = _three_step_session(fake_freecad)
    fake_freecad.undo_count = 1  # addon could only undo 1 of 2
    resp = session_rollback_operation(fake_freecad, 1)
    data = _json(resp)
    assert data["undone_transactions"] == 1
    assert sess.step_count == 2  # log truncated to match reality
    assert data["warnings"]


def test_rollback_invalid_step(fake_freecad, isolated_home):
    _three_step_session(fake_freecad)
    resp = session_rollback_operation(fake_freecad, 99)
    assert "Invalid step" in _text(resp)


def test_redo_restores_steps(fake_freecad, isolated_home):
    sess = _three_step_session(fake_freecad)
    fake_freecad.objects_by_doc["Doc"] = ["A"]
    session_rollback_operation(fake_freecad, 1)
    fake_freecad.objects_by_doc["Doc"] = ["A", "B"]
    resp = session_redo_operation(fake_freecad, 1)
    data = _json(resp)
    assert data["restored_steps"] == [2]
    assert sess.step_count == 2
    assert len(sess.redo_buffer) == 1


def test_redo_without_buffer(fake_freecad, isolated_home):
    _three_step_session(fake_freecad)
    resp = session_redo_operation(fake_freecad, 1)
    assert "Nothing to redo" in _text(resp)


# --- lifecycle ------------------------------------------------------------------


def test_session_status_with_guidance(fake_freecad, isolated_home):
    _start_session(fake_freecad)
    fake_freecad.objects_by_doc["Doc"] = []
    resp = session_status_operation(fake_freecad)
    data = _json(resp)
    assert data["document_open"] is True
    assert data["next_steps"][0]["operation"] == "create_object"
    assert "display_text" in data


def test_session_status_requires_session(fake_freecad, isolated_home):
    resp = session_status_operation(fake_freecad)
    assert "No active session" in _text(resp)


def test_get_steps_and_notes(fake_freecad, isolated_home):
    _start_session(fake_freecad)
    session_add_note_operation("draft note", "observation")
    resp = session_get_steps_operation()
    data = _json(resp)
    assert data["notes"][0]["note"] == "draft note"


def test_pause_resume_list_cycle(fake_freecad, isolated_home):
    sess = _start_session(fake_freecad)
    sid = sess.session_id
    resp = session_pause_operation()
    assert _json(resp)["success"] is True
    assert get_current_session() is None

    resp = session_list_operation()
    data = _json(resp)
    assert data["sessions"][0]["session_id"] == sid
    assert data["current_session_id"] is None

    resp = session_resume_operation(fake_freecad, sid)
    assert _json(resp)["success"] is True
    assert get_current_session().session_id == sid


def test_resume_missing_session(fake_freecad, isolated_home):
    resp = session_resume_operation(fake_freecad, "nope")
    data = _json(resp)
    assert data["success"] is False


def test_complete_registers_pattern_and_saves(fake_freecad, isolated_home):
    sess = _three_step_session(fake_freecad)
    resp = session_complete_operation(
        fake_freecad,
        save=True,
        save_path="/tmp/model.FCStd",
        description="three box workflow",
        tags=["boxes"],
    )
    data = _json(resp)
    assert data["success"] is True
    assert data["saved_file"] == "/tmp/model.FCStd"
    assert sess.status == "completed"
    assert get_current_session() is None
    # pattern is retrievable
    hits = recall_patterns_operation("three box workflow")
    assert _json(hits)["patterns"][0]["pattern_id"] == data["pattern_id"]


def test_complete_without_save(fake_freecad, isolated_home):
    _start_session(fake_freecad)
    resp = session_complete_operation(fake_freecad)
    data = _json(resp)
    assert data["saved_file"] is None
    assert "save_document" not in fake_freecad.called_methods()


# --- pattern + inspect tools -----------------------------------------------------


def test_save_and_recall_pattern(isolated_home):
    resp = save_pattern_operation("loft pipe", "Loft two wires", code="Part.makeLoft")
    pid = _json(resp)["pattern_id"]
    hits = _json(recall_patterns_operation("loft"))
    assert hits["patterns"][0]["pattern_id"] == pid


def test_recall_patterns_empty_store(isolated_home):
    resp = recall_patterns_operation("anything")
    assert "No patterns match" in _text(resp)


def test_inspect_object_mode(fake_freecad, isolated_home):
    resp = inspect_freecad_operation(fake_freecad, "Doc", "Box")
    data = _json(resp)
    assert data["kind"] == "object"
    assert data["properties"] == {"Length": "App::PropertyLength"}


def test_inspect_api_mode(fake_freecad, isolated_home):
    resp = inspect_freecad_operation(fake_freecad, dotted_name="Part.makeLoft")
    data = _json(resp)
    assert data["kind"] == "api"


def test_inspect_failure(fake_freecad, isolated_home):
    fake_freecad.result_overrides["inspect_freecad"] = {
        "success": False,
        "error": "Object 'X' not found",
    }
    resp = inspect_freecad_operation(fake_freecad, "Doc", "X")
    assert "not found" in _text(resp)


# --- align_shapes session recording (regression: it commits a transaction) ---


def test_align_shapes_records_session_step(fake_freecad, isolated_home):
    from cadpilot.operations import align_shapes_operation

    sess = _start_session(fake_freecad)
    fake_freecad.objects_by_doc["Doc"] = ["Box1", "Box2"]
    resp = align_shapes_operation(
        fake_freecad,
        "Doc",
        "Box1",
        "face",
        0,
        "Box2",
        "face",
        0,
        mode="touch",
    )
    data = _json(resp)
    assert data["success"] is True
    assert "step #1" in data["step_note"]
    assert sess.step_count == 1
    step = sess.steps[0]
    assert step.operation == "align_shapes"
    assert step.objects_after == ["Box1", "Box2"]
    assert step.atomic is True


def test_align_shapes_without_session_not_recorded(fake_freecad, isolated_home):
    from cadpilot.operations import align_shapes_operation

    resp = align_shapes_operation(
        fake_freecad,
        "Doc",
        "Box1",
        "face",
        0,
        "Box2",
        "face",
        0,
        mode="center",
    )
    data = _json(resp)
    assert data["success"] is True
    assert "step_note" not in data


def test_align_shapes_failed_op_not_recorded(fake_freecad, isolated_home):
    from cadpilot.operations import align_shapes_operation

    sess = _start_session(fake_freecad)
    fake_freecad.result_overrides["align_shapes"] = {
        "success": False,
        "error": "Face index 9 out of range",
    }
    resp = align_shapes_operation(
        fake_freecad,
        "Doc",
        "Box1",
        "face",
        9,
        "Box2",
        "face",
        0,
        mode="touch",
    )
    data = _json(resp)
    assert data["success"] is False
    assert sess.step_count == 0
