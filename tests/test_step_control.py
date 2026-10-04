"""MCP-side step journal tools: step_plan / step_control and session drift.

The addon half (journal storage, execution, undo) is exercised against a live
FreeCAD; these tests pin the MCP layer's contract — argument validation, the
exact spec forwarded over XML-RPC, and how a missing/old addon is tolerated.
"""

import json

from cadpilot.operations.core import (
    _journal_snapshot,
    session_rollback_operation,
    session_status_operation,
    step_control_operation,
    step_plan_operation,
)
from cadpilot.session_state import (
    ModelingSession,
    save_session,
    set_current_session,
)


def _text(resp) -> str:
    return "\n".join(c.text for c in resp)


class _OldAddonConnection:
    """A stale Mod/ install: no journal RPCs at all."""


def test_step_plan_rejects_empty_list(fake_freecad):
    assert "non-empty" in _text(step_plan_operation(fake_freecad, "Doc", []))


def test_step_plan_rejects_step_without_operation(fake_freecad):
    out = _text(step_plan_operation(fake_freecad, "Doc", [{"obj_name": "Box"}]))
    assert "step 1" in out
    assert "operation" in out
    assert "journal_op" not in fake_freecad.called_methods()


def test_step_plan_forwards_set_plan_spec(fake_freecad):
    steps = [{"operation": "create_object", "obj_type": "Part::Box", "obj_name": "B"}]
    out = _text(step_plan_operation(fake_freecad, "Doc", steps, "base plate"))

    method, args, _kwargs = fake_freecad.calls[-1]
    assert method == "journal_op"
    assert args[0] == "Doc"
    assert args[1] == {"operation": "set_plan", "steps": steps, "description": "base plate"}
    assert "nothing applied yet" in out.lower()
    assert "base plate" in out


def test_step_plan_reports_addon_failure(fake_freecad):
    fake_freecad.result_overrides["journal_op"] = {"success": False, "error": "no such document"}
    out = _text(step_plan_operation(fake_freecad, "Doc", [{"operation": "create_object"}]))
    assert "no such document" in out


def test_step_control_rejects_unknown_action(fake_freecad):
    out = _text(step_control_operation(fake_freecad, "Doc", "explode"))
    assert "unknown action 'explode'" in out
    assert "run_next" in out
    assert "journal_op" not in fake_freecad.called_methods()


def test_step_control_forwards_every_parameter(fake_freecad):
    step_control_operation(
        fake_freecad,
        "Doc",
        "reexecute",
        index=3,
        params={"obj_properties": {"Length": 9}},
        force=True,
    )
    method, args, _kwargs = fake_freecad.calls[-1]
    assert method == "journal_op"
    assert args[0] == "Doc"
    assert args[1] == {
        "operation": "reexecute",
        "index": 3,
        "params": {"obj_properties": {"Length": 9}},
        "force": True,
        "confirm": False,
    }


def test_step_control_forwards_reset_confirm(fake_freecad):
    """reset must reach the addon with confirm=true, or it is refused there."""
    step_control_operation(fake_freecad, "Doc", "reset", confirm=True)
    assert fake_freecad.calls[-1][1][1]["confirm"] is True


def test_step_control_defaults_params_to_empty_dict(fake_freecad):
    step_control_operation(fake_freecad, "Doc", "status")
    assert fake_freecad.calls[-1][1][1]["params"] == {}


def test_step_control_forwards_review_actions(fake_freecad):
    """accept/reject/update/insert/replay ride the same spec channel."""
    for action in ("accept", "reject", "update", "insert", "replay"):
        step_control_operation(fake_freecad, "Doc", action, index=2, params={"reason": "bad"})
        spec = fake_freecad.calls[-1][1][1]
        assert spec["operation"] == action
        assert spec["index"] == 2
        assert spec["params"] == {"reason": "bad"}


def test_step_control_unknown_action_lists_review_verbs(fake_freecad):
    out = _text(step_control_operation(fake_freecad, "Doc", "explode"))
    for verb in ("accept", "reject", "update", "insert", "replay"):
        assert verb in out


def test_step_control_surfaces_addon_error(fake_freecad):
    fake_freecad.result_overrides["journal_op"] = {
        "success": False,
        "error": "cannot roll back across non-atomic step(s) [2]",
    }
    out = _text(step_control_operation(fake_freecad, "Doc", "rollback_to", index=0))
    assert "rollback_to" in out
    assert "non-atomic" in out


def test_step_control_status_returns_json(fake_freecad):
    payload = json.loads(_text(step_control_operation(fake_freecad, "Doc", "status")))
    assert payload["success"] is True


def test_journal_snapshot_tolerates_a_stale_addon():
    """An old Mod/ install has no journal RPC: the reader must not raise."""
    assert _journal_snapshot(_OldAddonConnection(), "Doc") is None


def test_journal_snapshot_returns_none_on_addon_error(fake_freecad):
    fake_freecad.errors["get_step_journal"] = RuntimeError("connection reset")
    assert _journal_snapshot(fake_freecad, "Doc") is None


def _open_session(doc_name: str) -> ModelingSession:
    sess = ModelingSession(session_id="s1", name="S", doc_name=doc_name)
    save_session(sess)
    set_current_session(sess)
    return sess


def test_session_status_reports_journal_drift(fake_freecad, isolated_home):
    _open_session("Doc")
    fake_freecad.documents.append("Doc")
    fake_freecad.result_overrides["get_step_journal"] = {
        "success": True,
        "document": "Doc",
        "count": 2,
        "done": 2,
        "planned": 0,
        "drift": True,
        "records": [],
    }
    payload = json.loads(_text(session_status_operation(fake_freecad)))

    assert payload["journal"]["drift"] is True
    assert any("undo stack" in r for r in payload["journal_risks"])


def test_session_status_does_not_false_positive_on_count_mismatch(fake_freecad, isolated_home):
    """The journal is document-lifetime (pre-session work, read-only
    execute_code) while the session log is session-lifetime — differing step
    counts are the NORMAL state, not a desync. The count comparison used to
    fire a permanent "out of sync" warning; only drift is actionable."""
    _open_session("Doc")
    fake_freecad.documents.append("Doc")
    fake_freecad.result_overrides["get_step_journal"] = {
        "success": True,
        "document": "Doc",
        "count": 12,
        "done": 12,
        "planned": 0,
        "drift": False,
        "records": [],
    }
    payload = json.loads(_text(session_status_operation(fake_freecad)))

    assert payload["journal"]["done"] == 12
    assert payload["journal_risks"] == []


def test_session_status_without_journal_support_is_unchanged(fake_freecad, isolated_home):
    _open_session("Doc")
    fake_freecad.documents.append("Doc")
    fake_freecad.errors["get_step_journal"] = RuntimeError("old addon")
    payload = json.loads(_text(session_status_operation(fake_freecad)))

    assert payload["journal"] is None
    assert payload["journal_risks"] == []


def test_session_rollback_warns_about_pre_rollback_drift(fake_freecad, isolated_home):
    """The journal must be read BEFORE the undo: the undo rewinds it too, so a
    post-undo read would report clean and hide the drift entirely."""
    sess = _open_session("Doc")
    sess.add_step("create_object", "a")
    sess.add_step("edit_object", "b")
    save_session(sess)
    fake_freecad.result_overrides["get_step_journal"] = {
        "success": True,
        "document": "Doc",
        "count": 2,
        "done": 2,
        "planned": 0,
        "drift": True,
        "records": [],
    }

    payload = json.loads(_text(session_rollback_operation(fake_freecad, 1)))

    assert any("drift" in w for w in payload["warnings"])
    methods = fake_freecad.called_methods()
    assert methods.index("get_step_journal") < methods.index("undo_transactions")


def test_insert_wraps_a_single_step_dict(fake_freecad):
    """A single step dict in params (the same shape update takes) is the
    natural form — it used to silently produce an empty steps list and a
    misleading 'inside executed history' error."""
    step = {"operation": "create_object", "obj_name": "B", "obj_type": "Part::Box"}
    resp = step_control_operation(fake_freecad, "Doc", "insert", index=1, params=step)
    assert json.loads(_text(resp))["success"] is True
    spec = fake_freecad.calls[-1][1][1]
    assert spec["steps"] == [step]


def test_insert_passes_a_steps_list_through(fake_freecad):
    steps = [
        {"operation": "create_object", "obj_name": "A", "obj_type": "Part::Box"},
        {"operation": "create_object", "obj_name": "B", "obj_type": "Part::Box"},
    ]
    resp = step_control_operation(fake_freecad, "Doc", "insert", index=1, params={"steps": steps})
    assert json.loads(_text(resp))["success"] is True
    spec = fake_freecad.calls[-1][1][1]
    assert spec["steps"] == steps


def test_insert_rejects_empty_params_before_the_rpc(fake_freecad):
    resp = step_control_operation(fake_freecad, "Doc", "insert", index=1, params={})
    assert "steps" in _text(resp)
    assert fake_freecad.calls == []
