"""Guards for the deep framework bugs a second multi-agent stress run exposed.

These are the *scheduler-level* invariants — the ones that decide whether the
journal, rollback and the audit can be trusted at all — rather than single
operations:

* the commit probe must not key on ``UndoCount``, which SATURATES at FreeCAD's
  undo cap (``MaxUndoSize``, 20 by default): at the cap a real commit leaves the
  count pinned, so every mutation after the ~20th was journaled as read-only and
  rollback/replay skipped real steps;
* the connectivity audit must survive the origin datums of a default PartDesign
  document (infinite bound box, visible) and must not let one unmeasurable pair
  kill the whole scan;
* deleting an object others are BUILT ON must be refused: FreeCAD leaves the
  consumer behind with its base cleared, turning a subtractive feature additive
  — a silently wrong solid reported as a successful delete;
* an axis-based joint cannot be expressed through a face+vertex ref on a
  cylindrical face (the parts land tangent while the residual reads 0.0);
* a read-only ``execute_code`` must not be appended AFTER a pending plan, which
  left the plan cursor unable to run anything;
* a pattern ``count`` expression that does not resolve must say so instead of
  silently evaluating to 0.

The addon cannot be imported without FreeCAD, so these parse the source.
"""

import ast
from pathlib import Path

_ADDON = Path(__file__).resolve().parents[1] / "addon" / "CADPilot" / "rpc_server"


def _tree(name):
    return ast.parse((_ADDON / name).read_text(encoding="utf-8"))


def _func(tree, name):
    return next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )


def _writer_body(tree, name):
    """The body that does the writing, following a thin wrapper's delegation.

    ``run_record`` now only activates the document (``with active_document(doc):
    return _run_record_locked(...)``) and the transaction lives in the inner
    function; these guards care about the code that writes, not the name it
    sits under.
    """
    func = _func(tree, name)
    for stmt in func.body:
        if isinstance(stmt, ast.With):
            for sub in stmt.body:
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Call):
                    callee = sub.value.func
                    if isinstance(callee, ast.Name):
                        return _func(tree, callee.id)
    return func


def _attrs(node) -> set[str]:
    """Attribute names (``x.UndoCount`` -> 'UndoCount')."""
    return {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}


def _strings(node) -> set[str]:
    """String literals — the shape ``getattr(obj, "UndoNames", None)`` takes."""
    return {
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _names(node) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _calls(node) -> set[str]:
    return {
        n.func.attr
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }


ENGINE = _tree("step_engine.py")
ASSEMBLY = _tree("assembly_ops.py")
FACTORY = _tree("object_factory.py")
JOINTS = _tree("joint_ops.py")
FEATURE = _tree("feature_ops.py")
RPC = _tree("rpc_server.py")


def test_commit_probe_survives_the_undo_cap():
    token = _func(ENGINE, "undo_token")
    assert "UndoNames" in _strings(token), (
        "at FreeCAD's undo cap a real commit leaves UndoCount pinned; UndoNames still moves"
    )
    # Every commit site must use the token, and none may compare a raw count.
    for tree, owner in (
        (RPC, "_run_op_with_screenshot"),
        (ENGINE, "downgrade_if_no_undo"),
        (ENGINE, "run_record"),
    ):
        body = _writer_body(tree, owner)
        assert "undo_token" in _attrs(body) or "undo_token" in _names(body), (
            f"{owner} must probe with undo_token"
        )
        assert "UndoCount" not in _strings(body) and "UndoCount" not in _attrs(body), (
            f"{owner} still compares the raw UndoCount, which saturates"
        )


def test_change_detection_covers_every_open_document():
    """execute_code may switch documents, so "what changed" cannot be read off
    the document that happened to be active when the call arrived."""
    assert "listDocuments" in _attrs(_func(ENGINE, "document_tokens"))
    assert "document_tokens" in _names(_func(ENGINE, "changed_documents"))
    body = _func(RPC, "execute_code")
    assert "document_tokens" in _attrs(body), "fingerprint every open document"
    assert "changed_documents" in _attrs(body)
    assert "foreign_changes" in _strings(body), (
        "a change made outside our transaction must be reported, not recorded"
    )


def test_audit_skips_work_geometry_and_isolates_bad_pairs():
    helper = _func(ASSEMBLY, "_auditable_shape")
    assert "BoundBox" in _attrs(helper), "origin datums have an infinite bound box"
    assert "isfinite" in _attrs(helper), "an infinite bound must be rejected"
    assert "Faces" in _attrs(helper), "an empty sketch has no faces to measure"
    assert "_auditable_shape" in _names(_func(ASSEMBLY, "verify_assembly"))
    # One unmeasurable pair must not abort the scan.
    body = _func(ASSEMBLY, "verify_assembly")
    assert "skipped_unmeasurable" in ast.unparse(body)
    assert len([n for n in ast.walk(body) if isinstance(n, ast.Try)]) >= 3, (
        "distToShape and common need per-pair guards"
    )


def test_delete_refuses_when_objects_depend_on_it():
    helper = _func(FACTORY, "_dependents")
    assert "InList" in _strings(helper), "dependents come from FreeCAD's InList"
    assert "_CONTAINER_TYPES" in _names(helper), (
        "the owning Body appears in EVERY feature's InList — excluding containers is what "
        "keeps the check from refusing every delete inside a Body"
    )
    assert "PartDesign::Body" in _strings(FACTORY)
    body = _func(FACTORY, "delete_object_gui")
    assert "_dependents" in _names(body)
    assert any(
        isinstance(n, ast.Return) and "Cannot delete" in ast.unparse(n) for n in ast.walk(body)
    ), "a delete with dependents must refuse, not silently corrupt the model"


def test_axis_joints_cannot_be_refs_on_cylindrical_faces():
    helper = _func(JOINTS, "_axis_ref_refusal")
    assert "Part::GeomPlane" in _strings(helper), "a planar ref is the only usable one"
    assert "_AXIS_JOINTS" in _names(helper)
    mate = _func(JOINTS, "_op_mate")
    assert "_axis_ref_refusal" in _names(mate)
    # It must run BEFORE the joint moves anything: refusing has no side effects,
    # while failing after solve() could leave the parts displaced.
    src = ast.unparse(mate)
    assert src.index("_axis_ref_refusal") < src.index("_make_joint")


def test_read_only_execute_code_appends_and_plan_removal_is_by_state():
    """Read-only inspections are APPENDED at the very end, never inserted
    before a pending plan: insertion shifted every planned step's index, so an
    MCP client that read "pocket = step 11" and then called reexecute(11) hit
    the inspection instead (live: JTest). Appending is safe because the plan
    cursor walks by STATE and run_to's upto bound is cursor-based — the old
    done_count >= upto comparison is what broke under trailing done records
    and motivated the insertion in the first place. Plan removal must key on
    STATE (drop_planned), not on slicing from planned_tail_start: with done
    records behind the plan the tail is no longer a trailing run, and the
    slice would remove nothing, letting a stale plan survive a commit."""
    body = _func(ENGINE, "append_execute_code")
    assert "append" in _calls(body)
    assert "insert" not in _calls(body)
    assert "drop_planned" in _calls(body), (
        "plan invalidation must remove planned records by STATE, not position"
    )
    assert "planned_tail_start" not in _attrs(body)
    commit = _func(ENGINE, "record_commit")
    assert "drop_planned" in _calls(commit)
    assert "planned_tail_start" not in _attrs(commit)
    run = _func(ENGINE, "_run_steps")
    src = ast.unparse(run)
    assert "next_planned" in _calls(run), "the plan cursor must walk by state"
    assert "rec.index > upto" in src, (
        "run_to's upto bound must be the cursor (next_planned().index > upto)"
    )
    assert "done_count(records) >= upto" not in src, (
        "the upto bound must never compare a done-count: trailing done records "
        "(appended inspections, a snapshot marker) break the comparison early "
        "with planned steps still waiting"
    )


def test_unresolved_count_expression_is_reported():
    helper = _func(FEATURE, "_pattern_count")
    assert "State" in _strings(helper), (
        "a misspelled alias leaves the probe Invalid with value 0 — it must be detected"
    )
    assert "Invalid" in _strings(helper)


def test_ghost_stack_entries_never_consume_a_rollback_slot():
    """FreeCAD 1.1.4 attributes an undo entry to the document that is ACTIVE at
    commit time, so another document's transaction lands here rendered
    "-> name". Popping a ghost is a no-op for this document: counting it made a
    rollback report steps it never undid (live-verified), and leaving it in the
    journal comparison degraded every reexecute to a full rebuild."""
    stack_op = _func(ENGINE, "_stack_op")
    assert "_is_ghost_entry" in _names(stack_op), "the undo loop must recognise ghosts"
    assert "ghosts_skipped" in _strings(stack_op), "the skipped count must reach the caller"
    journal_check = _func(ENGINE, "_stack_holds_journal")
    assert "_real_undo_names" in _names(journal_check), (
        "the journal comparison must look at the same non-ghost sequence the undo loop pops"
    )


def test_every_transaction_activates_its_own_document():
    """The producer half of the ghost-entry bug: a transaction must be recorded
    while its document is App-active, or every concurrent multi-document run
    parks ghost entries on the other document's stack."""
    rpc_wrapper = _func(RPC, "_run_op_with_screenshot")
    assert "active_document" in _names(rpc_wrapper) or "active_document" in _attrs(rpc_wrapper), (
        "_run_op_with_screenshot must activate the target across open/commit"
    )
    for owner in ("run_record", "_remove_objects"):
        assert "active_document" in _names(_func(ENGINE, owner)), (
            f"{owner} opens a transaction and must activate its document too"
        )


def test_redo_reports_an_empty_stack_instead_of_silent_success():
    """session_redo's addon half: redo_transactions(0 redone) used to answer
    success with count 0, and the session layer reported restored_steps=[] while
    the redo buffer never drained."""
    body = _func(RPC, "_undo_redo")
    assert any("nothing to redo" in s for s in _strings(body))
    src = ast.unparse(body)
    assert "out['success'] = False" in src or 'out["success"] = False' in src


def test_snapshot_does_not_claim_a_mutation():
    """blocking = not atomic AND mutated; a snapshot is a marker that changes no
    geometry, so claiming a mutation made it a rollback blocker and the accepted
    soft-lock message became unreachable."""
    body = _func(ENGINE, "_snapshot")
    ctor = next(
        n
        for n in ast.walk(body)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "StepRecord"
    )
    flags = {
        kw.arg: ast.literal_eval(kw.value)
        for kw in ctor.keywords
        if isinstance(kw.value, ast.Constant)
    }
    assert flags.get("atomic") is False
    assert flags.get("mutated") is False, "a marker with no transaction must not claim a mutation"
    assert flags.get("accepted") is True
