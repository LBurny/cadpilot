"""Tests for session_state: step log, rollback bookkeeping, persistence."""

from cadpilot.session_state import (
    Step,
    get_current_session,
    list_sessions,
    load_session,
    new_session,
    save_session,
    set_current_session,
)


def test_add_step_numbers_and_clears_redo(isolated_home):
    sess = new_session("test", "Doc")
    s1 = sess.add_step("create_object", "create box", objects_after=["Box"])
    s2 = sess.add_step("edit_object", "edit box", objects_after=["Box"])
    assert (s1.step_number, s2.step_number) == (1, 2)
    assert s1.atomic is True

    sess.truncate_to(1)
    assert len(sess.redo_buffer) == 1
    # a new step invalidates the redo buffer (matches FreeCAD redo semantics)
    sess.add_step("delete_object", "delete box", objects_after=[])
    assert sess.redo_buffer == []
    assert sess.steps[-1].step_number == 2


def test_truncate_and_restore(isolated_home):
    sess = new_session("test", "Doc")
    for i in range(3):
        sess.add_step("create_object", f"step {i + 1}", objects_after=[f"Obj{i}"])
    removed = sess.truncate_to(1)
    assert [s.step_number for s in removed] == [2, 3]
    assert sess.step_count == 1

    restored = sess.restore_steps(2)
    assert [s.step_number for s in restored] == [2, 3]
    assert sess.step_count == 3
    assert sess.redo_buffer == []


def test_non_atomic_steps_after(isolated_home):
    sess = new_session("test", "Doc")
    sess.add_step("create_object", "a")
    sess.add_step("execute_code", "b", atomic=False)
    sess.add_step("edit_object", "c")
    assert sess.non_atomic_steps_after(1) == [2]
    assert sess.non_atomic_steps_after(2) == []


def test_add_note(isolated_home):
    sess = new_session("test", "Doc")
    sess.add_step("create_object", "a")
    entry = sess.add_note("wall thickness is 2mm", "assumption")
    assert entry["after_step"] == 1
    assert entry["note_type"] == "assumption"


def test_persistence_round_trip(isolated_home):
    sess = new_session("roundtrip", "Doc")
    sess.add_step(
        "create_object",
        "create box",
        params_summary="Part::Box 'Box'",
        result_summary="created",
        objects_after=["Box"],
    )
    sess.add_note("a note", "observation")
    save_session(sess)

    loaded = load_session(sess.session_id)
    assert loaded is not None
    assert loaded.name == "roundtrip"
    assert loaded.doc_name == "Doc"
    assert loaded.step_count == 1
    assert loaded.steps[0].objects_after == ["Box"]
    assert loaded.notes[0]["note"] == "a note"


def test_load_missing_returns_none(isolated_home):
    assert load_session("does-not-exist") is None


def test_list_sessions_sorted_by_updated(isolated_home):
    a = new_session("a", "DocA")
    b = new_session("b", "DocB")
    save_session(a)
    save_session(b)
    sessions = list_sessions()
    assert {s["session_id"] for s in sessions} == {a.session_id, b.session_id}
    assert sessions[0]["session_id"] == b.session_id  # most recent first
    assert sessions[0]["step_count"] == 0


def test_current_session_registry(isolated_home):
    assert get_current_session() is None
    sess = new_session("x", "Doc")
    set_current_session(sess)
    assert get_current_session() is sess


def test_step_from_dict_defaults(isolated_home):
    step = Step.from_dict({"step_number": 1, "operation": "create_object"})
    assert step.atomic is True
    assert step.objects_after == []
