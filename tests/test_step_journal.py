"""Step journal model/arithmetic — pure logic, no FreeCAD needed."""

import sys
from pathlib import Path

_ADDON = Path(__file__).resolve().parents[1] / "addon" / "CADPilot"
if str(_ADDON) not in sys.path:
    sys.path.insert(0, str(_ADDON))

from rpc_server import step_journal as sj  # noqa: E402

OPS = {"create_object", "edit_object", "delete_object", "pad", "pocket"}


def _step(op="create_object", name="Box", **extra):
    return {"operation": op, "obj_name": name, **extra}


def test_roundtrip_preserves_records():
    recs = [sj.build_record(_step(), 1, sj.STATE_DONE, OPS)]
    assert sj.from_json(sj.to_json(recs)) == recs


def test_from_json_tolerates_garbage():
    assert sj.from_json(None) == []
    assert sj.from_json("") == []
    assert sj.from_json("{not json") == []
    assert sj.from_json("[1, 2]") == []
    assert sj.from_json('{"records": [{"index": 1, "operation": "pad"}]}')[0].operation == "pad"


def test_executable_flag_follows_operation():
    assert sj.build_record(_step("pad"), 1, sj.STATE_PLANNED, OPS).executable is True
    assert sj.build_record(_step("assemble"), 2, sj.STATE_PLANNED, OPS).executable is False


def test_set_plan_replaces_only_the_unexecuted_tail():
    recs = [sj.build_record(_step(), 1, sj.STATE_DONE, OPS)]
    sj.set_plan(recs, [_step("pad"), _step("pocket")], OPS)
    sj.set_plan(recs, [_step("fillet")], OPS)
    assert [r.index for r in recs] == [1, 2]
    assert recs[0].state == sj.STATE_DONE
    assert recs[1].operation == "fillet"
    assert recs[1].state == sj.STATE_PLANNED


def test_plan_rollback_counts_done_records_after_target():
    recs = [sj.build_record(_step(), i, sj.STATE_DONE, OPS) for i in (1, 2, 3)]
    recs[1].state = sj.STATE_PLANNED
    plan = sj.plan_rollback(recs, 0)
    assert plan["undo_count"] == 2  # only 1 and 3 actually ran
    assert plan["affected"] == [1, 3]


def test_plan_rollback_flags_non_atomic_steps():
    recs = [sj.build_record(_step(), 1, sj.STATE_DONE, OPS)]
    recs.append(sj.build_record(_step("execute_code"), 2, sj.STATE_DONE, OPS))
    recs[1].atomic = False
    assert sj.plan_rollback(recs, 0)["blocking"] == [2]


def test_last_atomic_done_skips_trailing_non_atomic_record():
    """Drift anchors on the last transaction-bearing step.

    A trailing execute_code record has no transaction, so anchoring on the
    last completed record outright would compare "" against the undo stack
    and mask a manual undo.
    """
    recs = [sj.build_record(_step(), 1, sj.STATE_DONE, OPS)]
    recs[0].transaction = "CADPilot: create_object Box"
    recs.append(sj.build_record(_step("execute_code"), 2, sj.STATE_DONE, OPS))
    recs[1].atomic = False

    assert sj.last_atomic_done(recs) is recs[0]


def test_last_atomic_done_ignores_planned_records():
    recs = [sj.build_record(_step(), 1, sj.STATE_PLANNED, OPS)]
    assert sj.last_atomic_done(recs) is None


def test_rewind_moves_only_completed_records_back_to_planned():
    recs = [sj.build_record(_step(), i, sj.STATE_DONE, OPS) for i in (1, 2, 3)]
    back = sj.rewind(recs, 2)
    assert [r.index for r in back] == [2, 3]
    assert [r.state for r in recs] == [sj.STATE_DONE, sj.STATE_PLANNED, sj.STATE_PLANNED]
    assert sj.done_count(recs) == 1


def test_next_planned_and_counts():
    recs = [
        sj.build_record(_step(), 1, sj.STATE_DONE, OPS),
        sj.build_record(_step("pad"), 2, sj.STATE_PLANNED, OPS),
        sj.build_record(_step("pocket"), 3, sj.STATE_PLANNED, OPS),
    ]
    assert sj.next_planned(recs).index == 2
    assert sj.pending_count(recs) == 2
    assert sj.done_count(recs) == 1


# --- review semantics (accept / reject / update / insert) --------------------


def test_roundtrip_preserves_review_fields():
    rec = sj.build_record(_step(), 1, sj.STATE_DONE, OPS)
    rec.accepted, rec.note, rec.result, rec.duration_ms = True, "why", "Box", 12
    assert sj.from_json(sj.to_json([rec]))[0] == rec


def test_meta_roundtrip_and_absent_in_old_journals():
    recs = [sj.build_record(_step(), 1, sj.STATE_DONE, OPS)]
    text = sj.to_json(recs, {"description": "L profile"})
    assert sj.meta_from_json(text) == {"description": "L profile"}
    assert sj.from_json(text) == recs  # meta does not disturb the records
    assert sj.meta_from_json('{"records": []}') == {}
    assert sj.meta_from_json("{not json") == {}
    assert sj.meta_from_json(None) == {}


def test_plan_reject_drops_from_index_onward():
    recs = [sj.build_record(_step(), i, sj.STATE_DONE, OPS) for i in (1, 2, 3)]
    recs += [sj.build_record(_step("pad"), 4, sj.STATE_PLANNED, OPS)]
    plan = sj.plan_reject(recs, 2)
    assert plan["undo_count"] == 2  # done steps 2 and 3
    assert plan["drop"] == [2, 3, 4]  # the planned tail goes too
    assert plan["blocking"] == [] and plan["accepted"] == []


def test_plan_reject_planned_step_needs_no_undo():
    recs = [sj.build_record(_step(), 1, sj.STATE_DONE, OPS)]
    recs += [sj.build_record(_step("pad"), i, sj.STATE_PLANNED, OPS) for i in (2, 3)]
    plan = sj.plan_reject(recs, 2)
    assert plan["undo_count"] == 0 and plan["drop"] == [2, 3]
    assert sj.plan_reject(recs, 99) is None


def test_plan_reject_flags_blocking_and_accepted():
    recs = [sj.build_record(_step(), i, sj.STATE_DONE, OPS) for i in (1, 2)]
    recs[1].atomic, recs[1].accepted = False, True
    plan = sj.plan_reject(recs, 2)
    assert plan["blocking"] == [2] and plan["accepted"] == [2]


def test_set_accepted_only_marks_done_steps():
    recs = [
        sj.build_record(_step(), 1, sj.STATE_DONE, OPS),
        sj.build_record(_step(), 2, sj.STATE_PLANNED, OPS),
    ]
    assert sj.set_accepted(recs, 1).accepted is True
    assert sj.set_accepted(recs, 1, on=False).accepted is False
    assert sj.set_accepted(recs, 2) is None
    assert sj.set_accepted(recs, 9) is None


def test_update_planned_merges_params_and_label():
    rec = sj.build_record(_step(), 1, sj.STATE_PLANNED, OPS)
    rec.params = {"obj_name": "Box", "obj_properties": {"Length": 10}}
    assert sj.update_planned([rec], 1, {"obj_properties": {"Length": 20}}, "bigger") is rec
    assert rec.params["obj_properties"] == {"Length": 20}  # replaced wholesale
    assert rec.params["obj_name"] == "Box"  # top-level merge
    assert rec.label == "bigger"


def test_update_planned_rejects_done_and_unknown():
    done = sj.build_record(_step(), 1, sj.STATE_DONE, OPS)
    assert sj.update_planned([done], 1, {"obj_name": "X"}) is None
    assert sj.update_planned([done], 9, {}) is None


def test_insert_steps_only_into_the_planned_tail():
    recs = [
        sj.build_record(_step(), 1, sj.STATE_DONE, OPS),
        sj.build_record(_step("pad"), 2, sj.STATE_PLANNED, OPS),
    ]
    assert sj.insert_steps(recs, 0, [_step("pocket")], OPS) is None  # inside history
    added = sj.insert_steps(recs, 1, [_step("pocket", "P")], OPS)  # tail start
    assert [r.index for r in added] == [2]
    assert [r.operation for r in recs] == ["create_object", "pocket", "pad"]
    assert all(r.index == i for i, r in enumerate(recs, 1))
    assert sj.insert_steps(recs, len(recs), [_step("fillet")], OPS)  # append
    assert recs[-1].operation == "fillet"


def test_plan_rollback_reports_accepted():
    recs = [sj.build_record(_step(), i, sj.STATE_DONE, OPS) for i in (1, 2, 3)]
    recs[1].accepted = True
    assert sj.plan_rollback(recs, 0)["accepted"] == [2]


def test_invalidates_plan_only_when_objects_changed():
    """A read-only execute_code must not destroy a pending plan.

    Recording an execute_code commit used to drop the planned tail
    unconditionally, so merely inspecting the model silently deleted the plan
    the user was about to release.
    """
    recs = [sj.build_record(_step(), 1, sj.STATE_DONE, OPS)]
    recs[0].objects_after = ["Box"]
    recs += [sj.build_record(_step("pad"), 2, sj.STATE_PLANNED, OPS)]

    assert sj.invalidates_plan(recs, ["Box"]) is False  # inspection only
    assert sj.invalidates_plan(recs, ["Box", "Cut"]) is True  # model moved
    assert sj.invalidates_plan([], ["Box"]) is False  # nothing to compare against
