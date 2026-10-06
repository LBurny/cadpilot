"""Tests for the unified cad() dispatcher and modeling-session operations."""

import json

from cadpilot.operations import (
    cad_operation,
    execute_code_operation,
    inspect_freecad_operation,
    recall_patterns_operation,
    save_pattern_operation,
    session_action_operation,
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
    cad_operation(fake_freecad, "create_object", "Doc", obj_type="Part::Box", obj_name="Box")
    assert sess.steps[0].atomic is False


def test_cad_failure_not_recorded(fake_freecad, isolated_home):
    sess = _start_session(fake_freecad)
    fake_freecad.result_overrides["create_object"] = {"success": False, "error": "boom"}
    resp = cad_operation(fake_freecad, "create_object", "Doc", obj_type="Part::Box", obj_name="Box")
    assert "boom" in _text(resp)
    assert sess.step_count == 0


def test_cad_other_document_not_recorded(fake_freecad, isolated_home):
    sess = _start_session(fake_freecad, doc="Doc")
    resp = cad_operation(
        fake_freecad, "create_object", "OtherDoc", obj_type="Part::Box", obj_name="Box"
    )
    assert "not recorded" in _text(resp)
    assert sess.step_count == 0


def test_cad_no_session_runs_untracked(fake_freecad, isolated_home):
    resp = cad_operation(fake_freecad, "create_object", "Doc", obj_type="Part::Box", obj_name="Box")
    assert "created successfully" in _text(resp)
    assert "step #" not in _text(resp)


def test_cad_edit_and_delete_dispatch(fake_freecad, isolated_home):
    _start_session(fake_freecad)
    cad_operation(fake_freecad, "edit_object", "Doc", obj_name="Box", obj_properties={"Length": 5})
    cad_operation(fake_freecad, "delete_object", "Doc", obj_name="Box")
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
    resp = cad_operation(fake_freecad, "batch", "Doc", ops=ops)
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
    cad_operation(fake_freecad, "batch", "Doc", ops=[{"action": "create_object"}])
    assert sess.step_count == 0


def test_cad_unknown_operation(fake_freecad, isolated_home):
    resp = cad_operation(fake_freecad, "fly_to_moon", "Doc")
    assert "Unknown cad operation" in _text(resp)


def test_cad_requires_params(fake_freecad, isolated_home):
    resp = cad_operation(fake_freecad, "create_object", "Doc")
    assert "requires obj_type" in _text(resp)


# --- execute_code step accounting ----------------------------------------------


def test_execute_code_that_changed_the_document_is_an_atomic_step(fake_freecad, isolated_home):
    """The addon wraps a mutating snippet in a transaction, so the session
    records an atomic step and rollback can undo it. The current addon also
    reports WHICH document owns the transaction — only same-document changes
    are recorded (a foreign one would corrupt session_rollback)."""
    sess = _start_session(fake_freecad)
    fake_freecad.result_overrides["execute_code"] = {
        "success": True,
        "message": "Python code executed successfully.",
        "changed": True,
        "document": "Doc",
    }
    resp = execute_code_operation(fake_freecad, "b.Height = 10")
    assert "atomic step #1" in _text(resp)
    assert sess.steps[0].atomic is True


def test_execute_code_read_only_is_not_recorded(fake_freecad, isolated_home):
    """An inspection owns no transaction; recording it would break the session
    log's one-transaction-per-step invariant and block rollback. The fake's
    default result carries no `changed`, i.e. an older addon — conservative."""
    sess = _start_session(fake_freecad)
    resp = execute_code_operation(fake_freecad, "print(FreeCAD.listDocuments())")
    assert "read-only" in _text(resp)
    assert sess.step_count == 0


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


def test_rollback_skips_non_atomic_steps_without_blocking(fake_freecad, isolated_home):
    """A non-atomic step provably committed nothing (the addon's empty-commit
    probe), so it must neither demand force=true nor consume an undo pop: the
    rollback crosses it, undoes exactly the atomic count, and drops its log
    row. It used to refuse the whole rollback with a misattributed
    "execute_code without a transaction" message."""
    sess = _start_session(fake_freecad)
    sess.add_step("create_object", "a", objects_after=["A"])
    sess.add_step("execute_code", "b", atomic=False)
    resp = session_rollback_operation(fake_freecad, 0)
    data = _json(resp)
    assert data["success"] is True
    assert data["undone_transactions"] == 1  # only the atomic step's entry
    assert data["removed_steps"] == [1, 2]
    assert any("committed nothing" in w for w in data["warnings"])
    assert fake_freecad.calls[-1][:2] == ("undo_transactions", ("Doc", 1))


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


def test_session_start_captures_the_starting_object_set(fake_freecad, isolated_home):
    """Rolling back to step 0 must restore the state the session began with, so
    that state has to be recorded — otherwise the post-rollback check had
    nothing to compare against and reported success with objects left behind."""
    fake_freecad.objects_by_doc["Doc"] = ["Preexisting"]
    sess = _start_session(fake_freecad)
    assert sess.initial_objects == ["Preexisting"]


def test_rollback_to_zero_verifies_against_the_starting_state(fake_freecad, isolated_home):
    fake_freecad.objects_by_doc["Doc"] = ["Preexisting"]
    sess = _start_session(fake_freecad)
    sess.add_step("create_object", "a", objects_after=["Preexisting", "A"])
    sess.add_step("create_object", "b", objects_after=["Preexisting", "A", "B"])
    fake_freecad.objects_by_doc["Doc"] = ["Preexisting"]  # undo really restored it
    data = _json(session_rollback_operation(fake_freecad, 0))
    assert data["success"] is True
    assert data["state_matches_log"] is True, "to_step=0 used to skip the check entirely"
    assert not data["warnings"]


def test_rollback_to_zero_reports_objects_left_behind(fake_freecad, isolated_home):
    """The regression: a rollback whose undo came up short reported success with
    an empty warning list, because the verification was skipped at to_step=0.
    Success now means "the model reached the target state" — an object left
    behind is a failed rollback, not a successful one with a footnote."""
    fake_freecad.objects_by_doc["Doc"] = ["Preexisting"]
    sess = _start_session(fake_freecad)
    sess.add_step("create_object", "a", objects_after=["Preexisting", "A"])
    fake_freecad.objects_by_doc["Doc"] = ["Preexisting", "A"]  # A survived the undo
    data = _json(session_rollback_operation(fake_freecad, 0))
    assert data["success"] is False
    assert data["state_matches_log"] is False
    assert any("A" in w for w in data["warnings"])
    assert "ROLLBACK INCOMPLETE" in data["display_text"]


def test_rollback_to_zero_says_so_when_it_cannot_verify(fake_freecad, isolated_home):
    """A session resumed from an older file has no starting object set."""
    sess = _start_session(fake_freecad)
    sess.initial_objects = None
    sess.add_step("create_object", "a", objects_after=["A"])
    data = _json(session_rollback_operation(fake_freecad, 0))
    assert data["state_matches_log"] is None
    assert any("could not be verified" in w for w in data["warnings"])


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


def test_redo_with_an_empty_freecad_stack_fails_loudly(fake_freecad, isolated_home):
    """A diverged redo stack (the session buffer says there is work, FreeCAD
    holds none) used to answer success with restored_steps=[] and redo_remaining
    unchanged — a silent no-op loop. It must fail and keep the buffer."""
    sess = _three_step_session(fake_freecad)
    session_rollback_operation(fake_freecad, 1)
    before = len(sess.redo_buffer)
    assert before == 2
    fake_freecad.result_overrides["redo_transactions"] = {
        "success": True,
        "count": 0,
        "objects": ["A"],
    }
    text = _text(session_redo_operation(fake_freecad, 1))
    assert "restored nothing" in text
    assert len(sess.redo_buffer) == before


def test_rollback_surfaces_skipped_ghost_transactions(fake_freecad, isolated_home):
    """FreeCAD shares one undo stack across documents: other documents'
    transactions ride along as ghosts and the addon skips them without counting
    them. The reply must say so instead of hiding a mixed stack."""
    _three_step_session(fake_freecad)
    fake_freecad.objects_by_doc["Doc"] = ["A", "B"]
    fake_freecad.result_overrides["undo_transactions"] = {
        "success": True,
        "count": 1,
        "ghosts_skipped": 2,
        "objects": ["A", "B"],
    }
    data = _json(session_rollback_operation(fake_freecad, 2))
    assert data["success"] is True
    assert data["undone_transactions"] == 1
    assert any("other documents were skipped" in w for w in data["warnings"])


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


def test_session_actions_refuse_another_documents_session(fake_freecad, isolated_home):
    """The current session is ONE global slot per MCP server process, and several
    agents share that process: a status/rollback trusting the slot answers for —
    or undoes — the OTHER agent's document (live: an agent's rollback was refused
    with a stranger session's step range while its own document had 6 valid undo
    steps). A call that NAMES a document must be refused, not redirected."""
    _start_session(fake_freecad, doc="DocA")
    resp = session_action_operation(fake_freecad, "status", doc_name="DocB")
    text = _text(resp)
    assert "tracks document 'DocA'" in text, text
    assert "DocB" in text
    # Same document (and no document named at all) keeps working.
    assert "success" in _text(session_action_operation(fake_freecad, "status", doc_name="DocA"))
    assert "success" in _text(session_action_operation(fake_freecad, "status"))


def test_step_control_failure_carries_the_document_state(fake_freecad, isolated_home):
    """A bare "step 1 (batch): ValueError: …" hid that replay had already rolled
    the model back to step 0 (live: the document came out with 0 objects)."""
    from cadpilot.operations.core import step_control_operation

    fake_freecad.journal_op = lambda doc_name, spec: {
        "success": False,
        "error": "step 1 (batch): ValueError: bad alias",
        "warning": "replay rolled the document back to step 0 before re-running, so it "
        "now holds only what re-ran successfully (0 step(s))",
        "document_objects": [],
    }
    text = _text(step_control_operation(fake_freecad, "Doc", "replay"))
    assert "bad alias" in text
    assert "rolled the document back to step 0" in text
    assert "now holds: (nothing)" in text


def test_noop_transaction_is_not_recorded_as_atomic(fake_freecad, isolated_home):
    """``transaction: True`` only means a transaction was OPEN; ``undoable`` says
    an undo entry really landed. A same-value edit opens one and commits nothing,
    and counting it as atomic made session rollback pop one transaction too many
    — invisibly, because only properties had moved and the object-set check
    compares names."""
    sess = _start_session(fake_freecad)
    fake_freecad.objects_by_doc["Doc"] = ["Box"]
    fake_freecad.result_overrides["edit_object"] = {
        "success": True,
        "object_name": "Box",
        "transaction": True,
        "undoable": False,  # the addon downgraded it: nothing landed
        "objects": ["Box"],
    }
    cad_operation(fake_freecad, "edit_object", "Doc", obj_name="Box", obj_properties={"Height": 10})
    assert sess.step_count == 1
    assert sess.steps[0].atomic is False, "a commit with no undo entry must not count as a step"


def test_execute_code_session_step_records_the_object_fingerprint(fake_freecad, isolated_home):
    """Without the addon's object list an execute_code step's fingerprint was
    empty, so a rollback ending on such a step compared the model against an
    empty document and reported a false ROLLBACK INCOMPLETE."""
    sess = _start_session(fake_freecad)
    fake_freecad.result_overrides["execute_code"] = {
        "success": True,
        "changed": True,
        "document": "Doc",
        "attributed": True,
        "objects": ["Box", "Pin"],
        "message": "ok",
    }
    execute_code_operation(fake_freecad, "doc = App.ActiveDocument", doc_name="Doc")
    assert sess.step_count == 1
    assert sess.steps[0].objects_after == ["Box", "Pin"]


def test_session_rollback_asks_the_addon_to_refuse_foreign_entries(fake_freecad, isolated_home):
    """The session cannot name the transactions it expects (its step numbers are
    not journal indices), so the addon must refuse to pop entries that are not
    CADPilot's own: a manual GUI edit sitting on top was otherwise consumed in a
    step's place, silently, because only properties had moved."""
    # The session's starting fingerprint must include the box, or the forced
    # rollback below reports a mismatch for a rollback that worked.
    fake_freecad.objects_by_doc["Doc"] = ["Box"]
    sess = _start_session(fake_freecad)
    cad_operation(fake_freecad, "edit_object", "Doc", obj_name="Box", obj_properties={"Height": 10})
    fake_freecad.result_overrides["undo_transactions"] = {
        "success": False,
        "error": "1 of the 1 transaction(s) the undo would pop were not created by CADPilot: "
        "['Box.Height']. A manual FreeCAD edit or another tool's write owns them",
        "foreign_entries": ["Box.Height"],
    }
    text = _text(session_rollback_operation(fake_freecad, 0))
    assert "not created by CADPilot" in text
    calls = [c for c in fake_freecad.calls if c[0] == "undo_transactions"]
    assert calls[-1][1] == ("Doc", 1) and calls[-1][2].get("trust_journal") is True
    assert sess.step_count == 1, "a refused rollback must not truncate the session log"

    # force=True proceeds: the caller takes the risk, said so, and the check is
    # not sent (the addon then behaves exactly as before the guard).
    fake_freecad.result_overrides.pop("undo_transactions")
    assert _json(session_rollback_operation(fake_freecad, 0, force=True))["success"] is True
    assert [c for c in fake_freecad.calls if c[0] == "undo_transactions"][-1][1] == ("Doc", 1)
    assert sess.step_count == 0


def test_persistence_failure_degrades_to_a_warning(fake_freecad, isolated_home, monkeypatch):
    """A save_session OSError used to escape the tool AFTER the mutation had
    committed: the caller saw a hard protocol error for a change that actually
    succeeded, and would likely retry it (double-apply). It must degrade to a
    warning in the result."""
    import cadpilot.session_state as ss

    sess = _start_session(fake_freecad)

    def boom(_session):
        raise OSError("disk full")

    monkeypatch.setattr(ss, "save_session", boom)
    resp = cad_operation(fake_freecad, "create_object", "Doc", obj_type="Part::Box", obj_name="Box")
    text = _text(resp)
    assert "created" in text  # the mutation itself succeeded
    assert "could not be persisted" in text  # the persistence loss is visible
    assert sess.step_count == 1
