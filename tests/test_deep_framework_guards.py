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
        body = _func(tree, owner)
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


def test_read_only_execute_code_does_not_land_after_a_pending_plan():
    body = _func(ENGINE, "append_execute_code")
    assert "planned_tail_start" in _attrs(body)
    assert "insert" in _calls(body), (
        "the record must be INSERTED before the planned tail; appending it after left the "
        "plan cursor unable to run anything"
    )
    assert "append" not in _calls(body)


def test_unresolved_count_expression_is_reported():
    helper = _func(FEATURE, "_pattern_count")
    assert "State" in _strings(helper), (
        "a misspelled alias leaves the probe Invalid with value 0 — it must be detected"
    )
    assert "Invalid" in _strings(helper)


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
