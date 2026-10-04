"""Step journal model/arithmetic — pure logic, no FreeCAD needed."""

import ast
import math
import sys
from pathlib import Path

_ADDON = Path(__file__).resolve().parents[1] / "addon" / "CADPilot"
if str(_ADDON) not in sys.path:
    sys.path.insert(0, str(_ADDON))

from rpc_server import step_journal as sj  # noqa: E402

OPS = {"create_object", "edit_object", "delete_object", "pad", "pocket"}


def _step(op="create_object", name="Box", **extra):
    return {"operation": op, "obj_name": name, **extra}


def test_sub_operation_tolerates_action_keyed_batch_ops():
    """cad() batches journal sub-ops verbatim as {"action": ...} (the RPC
    schema); re-running such a step must still resolve the op name — the
    flange-rollback bug was "" -> "operation '' is not re-executable"."""
    assert sj.sub_operation({"operation": "fillet"}) == "fillet"
    assert sj.sub_operation({"action": "create_object"}) == "create_object"
    assert sj.sub_operation({"operation": "pad", "action": "ignored"}) == "pad"
    assert sj.sub_operation({}) == ""


def test_batch_executor_tolerates_operation_keyed_sub_ops():
    """The execution half of the same contract: _run_one_operation must read
    the sub-op name from "action" OR "operation" — journal-native batches carry
    the latter and must not die "unknown action: None" on their INITIAL run
    (they already replayed fine thanks to sub_operation). rpc_server imports
    FreeCAD, so this parses the source instead of importing it."""
    tree = ast.parse((_ADDON / "rpc_server" / "rpc_server.py").read_text(encoding="utf-8"))
    func = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_run_one_operation"
    )
    assigns = [
        n
        for n in ast.walk(func)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "action" for t in n.targets)
    ]
    keys = {
        n.value
        for a in assigns
        for n in ast.walk(a.value)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert {"action", "operation"} <= keys


def test_effect_label_summarises_a_snippet():
    """An execute_code row should say what the snippet DID, not echo its first
    line of boilerplate (`import FreeCAD` / `doc = FreeCAD.getDocument(...)`)."""
    assert sj.effect_label(False, ["A"], ["A"]) == "read-only"
    assert sj.effect_label(True, ["A"], ["A", "B"]) == "+1 object(s): B"
    assert sj.effect_label(True, ["A"], ["A", "B", "C", "D", "E"]).startswith(
        "+4 object(s): B, C, D"
    )
    assert sj.effect_label(True, ["A", "B"], ["A"]) == "-1 object(s)"
    assert sj.effect_label(True, ["A"], ["A"]) == "changed properties"


def test_snippet_description_reads_the_leading_comment_block():
    """The execute_code description convention: the snippet's LEADING comment
    block is the step's human description — the steps panel shows it in the
    row, the tooltip and (on double-click) the log."""
    code = "# 步骤1: 琴身轮廓 + f孔\n# 样条曲线插值\nimport FreeCAD\n"
    assert sj.snippet_description(code) == "步骤1: 琴身轮廓 + f孔\n样条曲线插值"


def test_snippet_description_skips_shebang_and_coding_lines():
    """Boilerplate lines are not description: shebang and the PEP-263 coding
    cookie (what the MCP client prepends) are skipped before the block."""
    code = "# -*- coding: utf-8 -*-\n# Cut the f-holes\nimport FreeCAD\n"
    assert sj.snippet_description(code) == "Cut the f-holes"
    assert sj.snippet_description("#!/usr/bin/env python3\n# Body\nx = 1\n") == "Body"


def test_snippet_description_stops_at_the_first_blank_or_code_line():
    """Only the leading block: a later comment is an implementation note."""
    code = "# Title\n\n# not part of the description\nimport FreeCAD\n"
    assert sj.snippet_description(code) == "Title"
    assert sj.snippet_description("# A\nx = 1\n# B\n") == "A"


def test_snippet_description_is_empty_without_a_leading_comment():
    assert sj.snippet_description("import FreeCAD\n# trailing\n") == ""
    assert sj.snippet_description("") == ""
    assert sj.snippet_description(None) == ""
    assert sj.snippet_description("# coding: utf-8\nimport Part\n") == ""


def test_step_description_prefers_the_snippet_comment_for_execute_code():
    rec = sj.StepRecord(
        index=1,
        operation="execute_code",
        label="execute_code: +1 object(s): Body",
        params={"code": "# 琴身轮廓\nimport FreeCAD\n"},
    )
    assert sj.step_description(rec) == "琴身轮廓"


def test_step_description_uses_the_label_for_other_ops():
    """Every other op's recorded label IS its human description."""
    rec = sj.StepRecord(index=1, operation="pad", label="pad 'Pad'")
    assert sj.step_description(rec) == "pad 'Pad'"


def test_step_description_is_empty_for_an_uncommented_snippet():
    rec = sj.StepRecord(
        index=1,
        operation="execute_code",
        label="execute_code: read-only",
        params={"code": "import FreeCAD\n"},
    )
    assert sj.step_description(rec) == ""


def test_row_text_leads_with_the_description():
    """An execute_code row is identifiable by its description, with the effect
    kept behind it — a column of 'execute_code: +1 object(s)…' rows told the
    user nothing."""
    rec = sj.StepRecord(
        index=3,
        operation="execute_code",
        label="execute_code: +1 object(s): Body",
        params={"code": "# 步骤1: 琴身轮廓 + f孔\n# 样条曲线\nimport FreeCAD\n"},
    )
    assert sj.row_text(rec) == "步骤1: 琴身轮廓 + f孔 · +1 object(s): Body"


def test_row_text_falls_back_to_the_label():
    """No comment (or another op): the label is the row, unchanged."""
    bare = sj.StepRecord(
        index=1,
        operation="execute_code",
        label="execute_code: read-only",
        params={"code": "import FreeCAD\n"},
    )
    assert sj.row_text(bare) == "execute_code: read-only"
    pad = sj.StepRecord(index=2, operation="pad", label="pad 'Pad'")
    assert sj.row_text(pad) == "pad 'Pad'"


def test_row_text_appends_the_error():
    rec = sj.StepRecord(index=4, operation="pad", label="pad 'Pad'", error="ValueError: boom")
    assert sj.row_text(rec).endswith("— ValueError: boom")


def test_tooltip_text_shows_the_whole_description_block():
    rec = sj.StepRecord(
        index=3,
        operation="execute_code",
        label="execute_code: +1 object(s): Body",
        params={"code": "# 琴身轮廓\n# 样条曲线插值\nimport FreeCAD\n"},
    )
    assert sj.tooltip_text(rec) == "琴身轮廓\n样条曲线插值\n+1 object(s): Body"


def test_tooltip_text_is_the_label_when_there_is_no_description():
    bare = sj.StepRecord(index=1, operation="execute_code", label="execute_code: read-only")
    assert sj.tooltip_text(bare) == "execute_code: read-only"
    pad = sj.StepRecord(index=2, operation="pad", label="pad 'Pad'")
    assert sj.tooltip_text(pad) == "pad 'Pad'"


def test_steps_without_undo_are_the_ones_a_rollback_leaves_behind():
    """The trigger for the whole fix: a step after the target that owns no
    transaction keeps its changes, so reporting plain success there is wrong."""
    ok = _fingerprinted("create_object", 1, [], ["A"])
    ok.transaction = "CADPilot: create A"
    old = _fingerprinted("execute_code", 2, ["A"], ["A", "B"], atomic=False)
    newer = _fingerprinted("create_object", 3, ["A", "B"], ["A", "B", "C"])
    newer.transaction = "CADPilot: create C"
    readonly = _fingerprinted("execute_code", 4, ["A", "B", "C"], ["A", "B", "C"], mutated=False)
    records = [ok, old, newer, readonly]
    assert sj.steps_without_undo(records, 0) == [2]
    assert sj.steps_without_undo(records, 2) == []
    assert sj.steps_without_undo(records, 3) == []


def test_blocking_text_names_the_real_operations():
    """The force prompt used to blame execute_code for every non-atomic blocker,
    but a snapshot marker blocks too, so naming the wrong op misleads."""
    recs = [
        sj.build_record(_step("execute_code"), 1, sj.STATE_DONE, OPS),
        sj.build_record(_step("snapshot"), 2, sj.STATE_DONE, OPS),
        sj.build_record(_step("snapshot"), 3, sj.STATE_DONE, OPS),
    ]
    text = sj.blocking_text(recs, [2, 3])
    assert "snapshot" in text and "execute_code" not in text
    assert "[2, 3]" in text


def _fingerprinted(op, index, before, after, **extra):
    rec = sj.build_record(_step(op), index, sj.STATE_DONE, OPS)
    rec.objects_before = list(before)
    rec.objects_after = list(after)
    for key, value in extra.items():
        setattr(rec, key, value)
    return rec


def test_created_since_lists_what_post_target_steps_added():
    """A step with no undo entry cannot be undone, but the journal still knows
    which objects it introduced (its before/after diff). This is what a
    rollback removes for real."""
    recs = [
        _fingerprinted("create_object", 1, [], ["Plate"]),
        _fingerprinted("create_object", 2, ["Plate"], ["Plate", "Rib"]),
        _fingerprinted("create_object", 3, ["Plate", "Rib"], ["Plate", "Rib", "Tab"]),
    ]
    assert sj.created_since(recs, 1) == ["Rib", "Tab"]
    assert sj.created_since(recs, 0) == ["Plate", "Rib", "Tab"]
    assert sj.created_since(recs, 3) == []


def test_created_since_keeps_objects_that_predate_the_journal():
    """Every objects_after snapshot lists the ENTIRE document, so subtracting
    whole snapshots (with an empty target at index 0) dragged the user's own
    objects into the removal set — a rebuild would have deleted them. The
    diffs name only what the journal built."""
    recs = [
        _fingerprinted("create_object", 1, ["Legacy"], ["Legacy", "New"]),
        _fingerprinted("create_object", 2, ["Legacy", "New"], ["Legacy", "New", "Extra"]),
    ]
    assert sj.created_since(recs, 0) == ["Extra", "New"]
    assert sj.created_since(recs, 1) == ["Extra"]


def test_created_since_falls_back_to_the_previous_done_snapshot():
    """A record without a before-list (hand-corrupted journal) must diff
    against the previous done record's after-list, not against an empty
    world — the fallback keeps objects the earlier steps already knew."""
    recs = [
        _fingerprinted("create_object", 1, [], ["Legacy"]),
        _fingerprinted("create_object", 2, [], ["Legacy", "Rib"]),
    ]
    assert sj.created_since(recs, 1) == ["Rib"]
    assert sj.created_since(recs, 0) == ["Legacy", "Rib"]


def test_unrecoverable_steps_flags_what_nothing_can_put_back():
    """Recorded before execute_code became transactional: no undo entry, no
    stored code, so neither undo nor a rebuild can restore it."""
    old = _fingerprinted("execute_code", 1, [], ["Ghost"], atomic=False, executable=False)
    later = _fingerprinted("execute_code", 2, ["Ghost"], ["Ghost", "Kept"], atomic=True)
    later.transaction = "CADPilot: execute_code"
    later.executable = True
    readonly = _fingerprinted("execute_code", 3, ["Ghost"], ["Ghost"], atomic=False, mutated=False)
    assert sj.unrecoverable_steps([old, later, readonly], 3) == [1]
    # A read-only step changed nothing, so it is not a problem.
    assert sj.unrecoverable_steps([readonly], 3) == []


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
    for r in (recs[0], recs[2]):
        r.transaction = f"CADPilot: step {r.index}"
    plan = sj.plan_rollback(recs, 0)
    assert plan["undo_count"] == 2  # only 1 and 3 actually ran
    assert plan["affected"] == [1, 3]


def test_undo_count_skips_records_without_a_transaction():
    """A done execute_code/snapshot record carries no transaction; counting it
    would undo a transaction belonging to an EARLIER step (plain stack)."""
    recs = [sj.build_record(_step(), i, sj.STATE_DONE, OPS) for i in (1, 2, 3)]
    recs[0].transaction = "CADPilot: create_object Box"
    recs[1].atomic = False  # execute_code: ran, but committed nothing
    recs[2].transaction = "CADPilot: fillet Box"
    assert sj.plan_rollback(recs, 0)["undo_count"] == 2
    assert sj.plan_reject(recs, 2)["undo_count"] == 1


def test_plan_rollback_flags_non_atomic_steps():
    recs = [sj.build_record(_step(), 1, sj.STATE_DONE, OPS)]
    recs.append(sj.build_record(_step("execute_code"), 2, sj.STATE_DONE, OPS))
    recs[1].atomic = False
    assert sj.plan_rollback(recs, 0)["blocking"] == [2]


def test_mutating_execute_code_counts_and_does_not_block():
    """A snippet that changed the document is wrapped in a transaction, so it
    counts for undo_count and is NOT a blocker — the whole point of the fix."""
    recs = [sj.build_record(_step(), 1, sj.STATE_DONE, OPS)]
    recs[0].transaction = "CADPilot: create_object Box"
    recs.append(sj.build_record(_step("execute_code"), 2, sj.STATE_DONE, OPS))
    recs[1].atomic = True
    recs[1].transaction = "CADPilot: execute_code"
    recs[1].executable = True
    plan = sj.plan_rollback(recs, 0)
    assert plan["undo_count"] == 2
    assert plan["blocking"] == []


def test_read_only_execute_code_neither_blocks_nor_counts():
    """An inspection changed nothing, so crossing it cannot revert the wrong
    change and it owns no transaction — it must not stand in rollback's way."""
    recs = [sj.build_record(_step(), 1, sj.STATE_DONE, OPS)]
    recs[0].transaction = "CADPilot: create_object Box"
    recs.append(sj.build_record(_step("execute_code"), 2, sj.STATE_DONE, OPS))
    recs[1].atomic = False
    recs[1].mutated = False
    plan = sj.plan_rollback(recs, 0)
    assert plan["undo_count"] == 1
    assert plan["blocking"] == []


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
    for r in recs:
        r.transaction = f"CADPilot: step {r.index}"
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


# --- manual-edit sync (pure half) -------------------------------------------


def _rec(op, index, name="Obj", props=None, before=None, after=None, state=sj.STATE_DONE):
    return sj.StepRecord(
        index=index,
        state=state,
        operation=op,
        params={"obj_name": name, "obj_properties": dict(props or {})},
        objects_before=list(before or []),
        objects_after=list(after or []),
    )


def test_tracked_objects_create_claims_scalars_and_placement():
    recs = [_rec("create_object", 1, "Box", {"Length": 10, "Shape": {"nested": 1}}, after=["Box"])]
    entry = sj.tracked_objects(recs)["Box"]
    # only scalar spec keys are claimable; non-scalar structure is never invented
    assert entry["props"]["Length"] == (1, "Length", None)
    assert "Shape" not in entry["props"]
    assert entry["props"]["Placement"] == (1, "Placement", None)
    assert entry["move"] is None and entry["sheet"] is None and entry["sketch"] is None


def test_tracked_objects_later_done_step_wins_per_property():
    recs = [
        _rec("create_object", 1, "Box", {"Length": 10}, after=["Box"]),
        _rec("edit_object", 2, "Box", {"Length": 20}),
    ]
    assert sj.tracked_objects(recs)["Box"]["props"]["Length"] == (2, "Length", None)


def test_tracked_objects_ignores_planned_steps():
    recs = [_rec("create_object", 1, "Box", {"Length": 10}, after=["Box"], state=sj.STATE_PLANNED)]
    assert sj.tracked_objects(recs) == {}


def test_tracked_objects_move_owns_the_final_pose():
    """A move is relative: the create step's absolute Placement must NOT claim
    the object (a re-run would double-apply), and the LAST move step is where a
    manual drag folds in."""
    recs = [
        _rec("create_object", 1, "Box", {"Length": 10}, after=["Box"]),
        _rec("move", 2, "Box", {"translate": {"x": 5, "y": 0, "z": 0}}),
        _rec("pad", 3, "Sketch", {"length": 8}, before=["Box"], after=["Box", "Pad"]),
        _rec("move", 4, "Box", {"translate": {"x": 0, "y": 5, "z": 0}}),
    ]
    entry = sj.tracked_objects(recs)["Box"]
    assert "Placement" not in entry["props"]
    assert entry["move"] == (4, None)  # the LAST move step, top-level


def test_tracked_objects_feature_claims_builder_keys_even_when_unpassed():
    """A pad built with the default length has no 'length' in params, but a GUI
    Length edit must still sync — the claim map is the builder's key set, not
    the caller's."""
    recs = [_rec("pad", 1, "Sketch", {}, before=["Sketch"], after=["Sketch", "Pad"])]
    props = sj.tracked_objects(recs)["Pad"]["props"]
    assert props["Length"] == (1, "length", None)
    assert props["Reversed"] == (1, "reversed", None)
    assert props["Midplane"] == (1, "midplane", None)


def test_tracked_objects_variables_claims_the_sheet_even_when_idempotent():
    """A variables re-run on an existing sheet creates nothing, so the
    before/after diff is empty — the sheet must be tracked by obj_name or its
    cells would never sync."""
    recs = [
        _rec("variables", 1, "Vars", {"cells": {"A1": ["w", 10]}}, before=["Vars"], after=["Vars"])
    ]
    entry = sj.tracked_objects(recs)["Vars"]
    assert entry["sheet"] == 1
    # the Body a sketch op created incidentally must NOT claim the sketch handlers
    recs = [
        _rec(
            "sketch",
            1,
            "Sketch",
            {"geometry": [], "constraints": []},
            before=[],
            after=["Body", "Sketch"],
        )
    ]
    tracked = sj.tracked_objects(recs)
    assert tracked["Sketch"]["sketch"] == 1
    assert tracked["Body"]["sketch"] is None


def test_tracked_objects_sketch_and_datum_claim_attachment_offset():
    recs = [
        _rec("sketch", 1, "Sketch", {"constraints": []}, after=["Sketch"]),
        _rec("datum_plane", 2, "DP", {"plane": "XY"}, before=["Sketch"], after=["Sketch", "DP"]),
    ]
    tracked = sj.tracked_objects(recs)
    assert tracked["Sketch"]["props"]["AttachmentOffset"] == (1, "offset", None)
    assert tracked["DP"]["props"]["AttachmentOffset"] == (2, "offset", None)


def test_map_cell_value_number_text_formula():
    # numeric cell: bare numeric content
    assert sj.map_cell_value(10, "25") == 25
    assert sj.map_cell_value(10, "7.5") == 7.5
    # text cell: a LEADING apostrophe marks text (trailing one optional), and
    # the builder's quoting persists in the cell text — strip both layers.
    # (live-measured on 1.1.4 by ordinals: getContents(text) = '"plate" with
    # a leading ' and NO trailing one.)
    assert sj.map_cell_value("hello", "'hello") == "hello"
    no_trailing = "'" + '"plate"'
    with_trailing = "'" + '"plate"' + "'"
    assert sj.map_cell_value("plate", no_trailing) == "plate"
    assert sj.map_cell_value("plate", with_trailing) == "plate"
    # user typed a number over a text cell, or text over a number cell
    assert sj.map_cell_value("hello", "25") == 25
    assert sj.map_cell_value(10, "'abc") == "abc"
    # formula: whitespace-normalised so FreeCAD's pretty-print compares equal
    assert sj.map_cell_value("=A1*2", "=A1 * 2") == "=A1*2"
    assert sj.map_cell_value("=A1*2", "25") == 25
    assert sj.map_cell_value(10, "=A1 * 2") == "=A1*2"
    # a cleared cell has no spec representation
    assert sj.map_cell_value(10, "") is sj.UNREADABLE
    assert sj.map_cell_value(10, None) is sj.UNREADABLE


def test_map_constraint_value_units_and_expressions():
    assert sj.map_constraint_value("distance", 10, 25.0, None) == 25
    # live angles read back in RADIANS; the spec takes degrees
    assert sj.map_constraint_value("angle", 45, math.pi / 2, None) == 90
    # a live expression binding wins over the literal, normalised
    assert sj.map_constraint_value("distance", 10, 25.0, "width / 2") == "=width/2"
    # unbound in the GUI: the datum number comes back
    assert sj.map_constraint_value("distance", "=width", 25.0, None) == 25
    assert sj.map_constraint_value("distance", 10, None, None) is sj.UNREADABLE


def test_params_for_passes_assembly_payloads_through():
    """Whitelisting keys used to strip an assemble step's mates, so a planned
    assemble could never run; everything but routing/presentation passes."""
    step = {
        "operation": "assemble",
        "description": "snap the plate on",
        "mates": [{"obj": "A", "anchor": "com", "target": "B", "target_anchor": "com"}],
        "tolerance": 0.2,
    }
    params = sj.params_for(step)
    assert params["mates"] == step["mates"]
    assert params["tolerance"] == 0.2
    assert "operation" not in params and "description" not in params


def test_document_refs_extracts_getdocument_literals():
    """Both quote styles count; a variable argument does not (nothing to
    rewrite) — the DeskFan rename bug hid in these literals."""
    code = (
        "doc = App.getDocument('MideaDeskFan')\n"
        'd2 = FreeCAD.getDocument("Other")\n'
        "d3 = App.getDocument(name)\n"
    )
    assert sj.document_refs(code) == {"MideaDeskFan", "Other"}


def test_rewrite_document_refs_repoints_only_mapped_names():
    code = "doc = App.getDocument('Old')  # Old\nFreeCAD.getDocument(\"Old\")\ngetDocument('Keep')"
    out = sj.rewrite_document_refs(code, {"Old": "New"})
    assert "getDocument('New')" in out
    assert "getDocument(\"New\")" in out
    assert "getDocument('Keep')" in out
    assert "App.getDocument" in out  # the accessor itself is untouched
    # unmapped names survive; empty mapping is a no-op
    assert sj.rewrite_document_refs(code, {"Missing": "X"}) == code
    assert sj.rewrite_document_refs(code, {}) == code
    assert sj.rewrite_document_refs("", {"A": "B"}) == ""


def _done_rec(op, index, params, before=(), after=()):
    return sj.StepRecord(
        index=index,
        state=sj.STATE_DONE,
        operation=op,
        label=op,
        params=params,
        atomic=True,
        mutated=True,
        executable=True,
        objects_before=list(before),
        objects_after=list(after),
    )


def test_tracked_objects_claims_batch_sub_ops():
    """Manual edits on batch-created objects must sync into the SUB-OP's
    obj_properties — the BatchA drag used to vanish from the journal, and a
    replay of the batch silently reverted the user's correction."""
    rec = _done_rec(
        "batch",
        7,
        {"ops": [
            {"action": "create_object", "obj_name": "BatchA",
             "obj_properties": {"Height": 6, "Length": 10, "Width": 10}},
            {"action": "move", "obj_name": "BatchA", "obj_properties": {"translate": [1, 0, 0]}},
            {"operation": "pad", "obj_name": "Pad1", "obj_properties": {"length": 5}},
        ]},
        before=["Old"],
        after=["Old", "BatchA", "Pad1"],
    )
    t = sj.tracked_objects([rec])
    # create sub-op claims its scalars + Placement, routed to sub 0
    assert t["BatchA"]["props"]["Height"] == (7, "Height", 0)
    # the batch's move SUB-OP owns the final pose: create claims no
    # Placement, the fold targets (step 7, sub 1)
    assert "Placement" not in t["BatchA"]["props"]
    assert t["BatchA"]["move"] == (7, 1)
    assert "Placement" not in t["BatchA"]["props"]
    # feature sub-ops are not claimed (no per-sub-op object attribution)
    assert "Pad1" not in t
    # later sub-op wins per property
    rec2 = _done_rec(
        "batch", 8,
        {"ops": [{"action": "edit_object", "obj_name": "BatchA",
                  "obj_properties": {"Length": 12}}]},
        after=["BatchA"],
    )
    t2 = sj.tracked_objects([rec, rec2])
    assert t2["BatchA"]["props"]["Length"] == (8, "Length", 0)


def test_drop_planned_removes_by_state_not_position():
    """Inspections append BEHIND the plan now, so the plan is not a trailing
    run: removal must key on state — slicing from the end left a stale plan
    alive after a commit (the snapshot-marker variant of the same bug)."""
    recs = [
        _done_rec("pad", 1, {}, after=["Pad"]),
        _done_rec("create_object", 2, {}, after=["Pad", "Box"]),
        sj.StepRecord(index=3, state=sj.STATE_PLANNED, operation="pocket"),
        sj.StepRecord(index=4, state=sj.STATE_DONE, operation="execute_code"),
        sj.StepRecord(index=5, state=sj.STATE_PLANNED, operation="fillet"),
    ]
    assert sj.drop_planned(recs) == 2
    assert [r.state for r in recs] == [sj.STATE_DONE, sj.STATE_DONE, sj.STATE_DONE]


def test_insert_steps_bounds_follow_the_planned_run_not_the_suffix():
    OPS = {"pocket", "fillet"}

    def fresh():
        return [
            _done_rec("create_object", 1, {}, after=["Box"]),
            _done_rec("pad", 2, {}, after=["Box", "Pad"]),
            sj.StepRecord(index=3, state=sj.STATE_PLANNED, operation="pocket"),
            sj.StepRecord(index=4, state=sj.STATE_PLANNED, operation="fillet"),
            sj.StepRecord(index=5, state=sj.STATE_DONE, operation="execute_code"),
        ]

    # run head (after the last done record) and run interior: fine
    assert sj.insert_steps(fresh(), 2, [_step(name="X")], OPS) is not None
    assert sj.insert_steps(fresh(), 4, [_step(name="X")], OPS) is not None
    # after the trailing inspection would strand planned steps behind a done one
    assert sj.insert_steps(fresh(), 5, [_step(name="X")], OPS) is None
    # before the run is history
    assert sj.insert_steps(fresh(), 1, [_step(name="X")], OPS) is None
    # no planned records: appending at the very end stays allowed
    done_only = [_done_rec("create_object", 1, {}, after=["Box"])]
    assert sj.insert_steps(done_only, 1, [_step(name="X")], OPS) is not None
    assert sj.insert_steps(done_only, 0, [_step(name="X")], OPS) is None


def test_set_plan_drops_planned_wherever_they_sit():
    recs = [
        _done_rec("pad", 1, {}, after=["Pad"]),
        sj.StepRecord(index=2, state=sj.STATE_PLANNED, operation="pocket"),
        sj.StepRecord(index=3, state=sj.STATE_DONE, operation="execute_code"),
        sj.StepRecord(index=4, state=sj.STATE_PLANNED, operation="fillet"),
    ]
    added = sj.set_plan(recs, [_step(name="New")], OPS)
    assert [r.index for r in added] == [3]
    assert [ (r.index, r.state, r.operation) for r in recs ] == [
        (1, sj.STATE_DONE, "pad"),
        (2, sj.STATE_DONE, "execute_code"),
        (3, sj.STATE_PLANNED, "create_object"),
    ]


def test_next_planned_walks_by_state_behind_done_records():
    recs = [
        _done_rec("pad", 1, {}, after=["Pad"]),
        sj.StepRecord(index=2, state=sj.STATE_PLANNED, operation="pocket"),
        sj.StepRecord(index=3, state=sj.STATE_DONE, operation="execute_code"),
        sj.StepRecord(index=4, state=sj.STATE_PLANNED, operation="fillet"),
    ]
    assert sj.next_planned(recs).index == 2
    assert sj.pending_count(recs) == 2
