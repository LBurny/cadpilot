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
* an axis-based joint on a cylindrical face used to land the parts TANGENT (a
  face+vertex ref has no axis; the residual still read 0.0) — the ref landing
  is now the face center/axis, and an axis joint names the axis it will use;
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


def test_axis_joints_name_the_axis_they_use_instead_of_landing_tangent():
    """A cylinder ref lands on the AXIS now (the face-center marker FreeCAD's
    own click rules use), so an axle-in-hole joint works; the old hard refusal
    was correct only while the landing was a seam vertex. What must survive is
    the visibility: an axis joint on a rotational face reports the axis it
    will use, and the residual for two rotational faces is axis-to-axis (the
    surface distance of an axle fit is its radial gap, e.g. 15 mm for a 5 mm
    pin in a 20 mm bore — a coaxial mate must not read as a 15 mm error)."""
    helper = _func(JOINTS, "_axis_ref_notes")
    assert "_AXIS_JOINTS" in _names(helper), "only axis joints need an axis"
    assert "_axis_of" in _names(helper), "the note names the surface's own axis"
    mate = _func(JOINTS, "_op_mate")
    assert "_axis_ref_notes" in _names(mate)
    assert "ValueError" not in ast.unparse(_func(JOINTS, "_axis_ref_notes")), (
        "a rotational ref is no longer refused — it lands on its axis"
    )
    residual = ast.unparse(_func(JOINTS, "_residual"))
    assert "axis" in residual and "_ROTATIONAL_SURFACES" in residual, (
        "two rotational faces measure axis-to-axis"
    )
    assert "_axis_ref_notes" not in _names(mate) or "_landing_warnings" in _names(mate), (
        "the axis note joins the landing warnings, not a separate channel"
    )


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
    assert "next_runnable" in _calls(run), (
        "the plan cursor must walk by state, and a FAILED record is still to be"
        " applied: its transaction aborted, so stepping over it runs the rest of"
        " the plan against a dependency that was never created"
    )
    assert "next_planned" not in _calls(run), (
        "next_planned skips failed records, which is the bug this replaced"
    )
    assert "rec.index > upto" in src, (
        "run_to's upto bound must be the cursor (next_runnable().index > upto)"
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
    journal_check = _func(ENGINE, "_undo_trust")
    assert "_real_undo_names" in _names(journal_check), (
        "the journal comparison must look at the same non-ghost sequence the undo loop pops"
    )


def test_a_capped_undo_stack_is_not_a_reopened_document():
    """The undo stack is CAPPED (``MaxUndoSize``, 20 by default), so a journal
    longer than the cap can never satisfy a whole-span comparison: the oldest
    entries were evicted by FreeCAD, while every surviving entry still matches
    the journal in order. Reading that as "does not hold the journal's
    transactions" popped nothing and forced a full rebuild of the whole journal
    on every rollback deeper than the cap (live-caught: a 24-transaction journal
    rolled back to 0 reported a reopened document, with the top 20 undo names
    identical to the journal's). The trust check is now a SPAN, computed in the
    pure journal model, and the message names the cap instead of guessing.
    """
    trust = _func(ENGINE, "_undo_trust")
    assert "undo_trust_span" in _attrs(trust) or "undo_trust_span" in _names(trust), (
        "the span must come from the pure journal model, where it is unit-tested"
    )
    assert "MaxUndoSize" in _strings(_func(ENGINE, "_undo_cap")), (
        "the short-stack message must name FreeCAD's undo cap, not blame a reopen"
    )
    # The span, not a yes/no: rollback must pop exactly what the stack holds.
    rollback = _func(ENGINE, "_rollback")
    assert "span" in _names(rollback) and "_undo_trust" in _names(rollback), (
        "_rollback must undo the span the stack really holds"
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


def test_undo_entry_names_are_unique_per_step():
    """Two property-only steps used to commit under the SAME undo name, so the
    journal's name comparison could not tell which transaction was on top. A
    manual Ctrl+Z of the newest step then left an older same-named entry on the
    stack, the drift check (a membership test) still answered "present", and a
    rollback popped the WRONG transaction while every object-set verification
    stayed silent — only properties had moved. Every opener must route through
    transaction_name so each entry names its step."""
    tname = _func(ENGINE, "transaction_name")
    assert "_TX_SEQ" in _names(tname), "the name must carry a per-document sequence"
    assert "TX_PREFIX" in _names(tname)
    assert "transaction_name" in _names(_writer_body(ENGINE, "_run_record_locked")), (
        "a re-run must name its entry through the same helper as a first commit"
    )
    literals = [
        arg
        for call in ast.walk(RPC)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "openTransaction"
        for arg in call.args
        if isinstance(arg, ast.Constant)
    ]
    assert not literals, f"openTransaction still called with a literal name: {literals}"
    # The session layer keeps its own log and cannot name the transactions it
    # expects, so the addon refuses entries that are not CADPilot's own instead
    # of eating a manual edit in a step's place.
    assert "foreign_undo_entries" in _attrs(_func(RPC, "_undo_redo")), (
        "undo/redo for a caller with its own log must refuse foreign entries"
    )


def test_modal_blocker_is_reported_and_dismissible_without_a_gui():
    """A client that cannot drive the GUI (Claude Code, opencode, ...) has no
    recovery from FreeCAD's document-recovery dialog unless CADPilot itself can
    name the blocker and clear it: every dispatched call is held back by design,
    so the dismissal has to bypass that guard, and the state probe must read a
    GUI-tick-refreshed global (the RPC thread must not touch Qt widget state).

    Live-verified: with a modal open the addon answered get_gui_state with
    defer_reason 'modal' before any call timed out, and dismiss_blocking_dialog
    closed it with Cancel semantics.
    """
    dispatch = _tree("gui_dispatch.py")
    probe = _func(dispatch, "backpressure")
    assert "_guard_state" in _names(probe), "an idle queue must still name the blocker"
    drain = _func(dispatch, "process_gui_tasks")
    assert "_probe_guard_state" in _names(drain), "the guard state is refreshed on the GUI tick"
    assert "_force_queue" in _names(drain), "a dismissal must run despite the guards"
    dismiss = _func(dispatch, "dismiss_blocking_dialog")
    assert "force" in {
        kw.arg
        for n in ast.walk(dismiss)
        if isinstance(n, ast.Call)
        for kw in getattr(n, "keywords", [])
    }, "the dismissal must go through the forced queue"
    assert "reject" in _attrs(dismiss), "Cancel semantics: reject(), never accept()"
    rpc = _func(RPC, "dismiss_blocking_dialog")
    assert "dispatch_to_gui" not in _names(rpc), "it must not be a normal dispatched call"


def test_observer_mute_counter_cannot_leak_negative():
    """`engine_quiet` is a counter, and a window left open across a hot reload
    runs its `__exit__` against the freshly re-executed (zeroed) counter. The
    unbalanced decrement left it at -1 PERMANENTLY, so every later
    `engine_quiet()` brought it to 0 instead of 1: the observer was no longer
    muted and synced machine writes into the owning step's params (live: after
    one `restart_rpc_server()` from execute_code, that snippet's own writes
    rewrote the batch step's obj_properties — the exact bleed the window exists
    to prevent). The exit clamps, and the observer tests `> 0`."""
    klass = next(
        n for n in ast.walk(ENGINE) if isinstance(n, ast.ClassDef) and n.name == "_EngineQuiet"
    )
    exit_fn = next(n for n in klass.body if isinstance(n, ast.FunctionDef) and n.name == "__exit__")
    src = ast.unparse(exit_fn)
    assert "max(0" in src, f"the quiet counter must clamp at zero, got: {src}"
    guard = [
        node
        for node in ast.walk(_func(ENGINE, "slotChangedObject"))
        if isinstance(node, ast.Compare)
    ]
    assert any(
        isinstance(c.left, ast.Name)
        and c.left.id == "_ENGINE_ACTIVE"
        and isinstance(c.ops[0], ast.Gt)
        for c in guard
    ), "the observer must treat only a POSITIVE counter as muted"


def test_expression_binding_changes_are_tracked():
    """Binding/unbinding an expression in the GUI rewrites ExpressionEngine and
    need not fire the bound property, so a step's params kept the last NUMBER
    (or kept "=Vars.x" after an unbind) while the model was already driven by
    the expression. The observer must re-read everything it tracks for the
    object when ExpressionEngine changes."""
    sync = _func(ENGINE, "_sync")
    assert "ExpressionEngine" in _strings(sync), "the observer must react to the engine itself"
    assert {"_sync_constraints", "_sync_cells", "_sync_move_fold"} <= _attrs(sync), (
        "every handler must re-read its values when ExpressionEngine changes"
    )


def test_sync_claims_follow_the_object_that_really_changed():
    """Two producer-side mismatches between a record and reality, both of which
    sent a manual edit nowhere (or to the wrong step):

    * a move of a PartDesign feature is applied to the owning BODY — FreeCAD
      rewrites a feature's Placement on every recompute — so claiming the
      requested feature left the Body untracked while the pose claim sat on an
      object that cannot hold one;
    * FreeCAD de-duplicates a requested create name (Box -> Box001), so a batch
      sub-op claiming the REQUESTED name overwrote the real object's claims and
      left the created one untracked.

    Both halves are required: the record must carry what the op did, and the
    claim must read it (the pure half is unit-tested in test_step_journal).
    """
    task = _func(RPC, "_task_body")
    assert "moved_object" in _strings(task), "the record must carry what the op moved"
    assert "params" in _names(task), "and it must reach record_commit's params"
    batch = _func(RPC, "_run_batch")
    assert {"moved_object", "created_object"} <= _strings(batch), (
        "a batch sub-op must record the object it created / moved, not the requested name"
    )


def test_object_removal_mutes_the_manual_edit_observer():
    """The removal window is engine-driven: the recompute after a removal fires
    changed-object events (a dependent feature's AttachmentOffset, a Body's tip)
    and mirroring those back into the journal rewrites a step's params from a
    state the removal itself is tearing down."""
    body = _func(ENGINE, "_remove_objects_locked")
    assert "_EngineQuiet" in _names(body) or "_EngineQuiet" in _attrs(body), (
        "removal must run inside _EngineQuiet"
    )


def test_native_rollback_replans_failed_records():
    """Both rollback paths must agree about what the journal means afterwards.
    The rebuild path has always reset done AND failed records to planned; the
    native undo path only rewound the done ones, so a failed step stayed failed,
    the cursor stepped over it, and the panel's Next ran the steps behind it
    against a dependency that was never created (live: a rollback to step 3,
    then Next skipped the failed pocket and pattern and ran the move and batch
    steps on a part missing both)."""
    rollback = ast.unparse(_func(ENGINE, "_rollback"))
    assert "replan_failed" in rollback, "a rollback must re-plan failed records"
    assert "replanned" in rollback, "and report which ones it re-planned"
    # The rebuild branch keeps its own reset (it also clears object state), and
    # the failed state must be part of that set too.
    assert "STATE_FAILED" in rollback
