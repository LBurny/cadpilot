"""FreeCAD-facing half of the step journal.

Owns the document property, per-step execution, and undo. Everything here
runs on the GUI thread.

``apply_op`` is the single entry point, shared by the RPC handler
(``FreeCADRPC.journal_op``) and the GUI panel — so the panel keeps working
with the RPC server stopped.

Journal writes happen INSIDE each step's FreeCAD transaction, so a step's log
entry and the model change it describes are committed together and cannot
diverge on failure.

They are NOT, however, restored by ``doc.undo()``: FreeCAD tracks undo for
document OBJECTS, and a document-level dynamic property like this journal is
not part of that. Verified on 1.1.4 — after ``doc.undo()`` the created object
was gone while the journal still reported the step as ``done``. Rollback and
re-execution therefore reconcile the log explicitly (``sj.rewind``); that
reconciliation is the primary mechanism, not a safety net. The ``drift`` flag
in :func:`_status` surfaces the case where someone undid work by hand.
"""

from __future__ import annotations

import contextlib
import math
import time
from typing import Any

import FreeCAD

from rpc_server import dbglog
from rpc_server import step_journal as sj
from rpc_server.feature_ops import FEATURE_TYPES, create_feature_gui
from rpc_server.object_factory import (
    create_object_gui,
    delete_object_gui,
    edit_object_gui,
    repair_body_tips,
)
from rpc_server.property_mapper import Object

logger = dbglog.get_logger("journal")

EXECUTABLE_OPS = (
    {"create_object", "edit_object", "delete_object", "batch"}
    | set(FEATURE_TYPES)
    | {
        # The assembly toolchain used to journal with params={} and
        # executable=False: rollback undid them (they own transactions), but a
        # replay/rebuild SKIPPED them and the model came back unassembled, and
        # their unrecoverable flag degraded every rebuild across them to
        # "partial". They carry their full payload now and re-run for real.
        "assemble",
        "align_shapes",
        "set_anchors",
        "assembly",
    }
)


# --- document property -------------------------------------------------------


def read_journal(doc) -> list[sj.StepRecord]:
    try:
        text = getattr(doc, sj.JOURNAL_PROP, "") or ""
    except Exception:
        text = ""
    return sj.from_json(text)


def read_meta(doc) -> dict[str, Any]:
    try:
        text = getattr(doc, sj.JOURNAL_PROP, "") or ""
    except Exception:
        text = ""
    return sj.meta_from_json(text)


def write_journal(doc, records: list[sj.StepRecord], meta: dict[str, Any] | None = None) -> None:
    sj.reindex(records)
    if sj.JOURNAL_PROP not in list(doc.PropertiesList):
        doc.addProperty("App::PropertyString", sj.JOURNAL_PROP, "CADPilot", "CADPilot step journal")
    if meta is None:
        meta = read_meta(doc)
    setattr(doc, sj.JOURNAL_PROP, sj.to_json(records, meta))


# --- small helpers -----------------------------------------------------------


def _undo_names(doc) -> list[str]:
    try:
        return list(getattr(doc, "UndoNames", []) or [])
    except Exception:
        return []


def _is_ghost_entry(name) -> bool:
    """A transaction belonging to ANOTHER document, parked on this stack.

    FreeCAD 1.1.4 attributes an undo entry to the document that is ACTIVE when
    the transaction is recorded, not to the one it was opened on: committing on
    a non-active document leaves a "-> <name>" entry on the active document's
    stack (live-verified with plain FreeCAD, no CADPilot involved). A ghost is
    not a step of this document; popping it via ``doc.undo()`` is a no-op here,
    so it must never consume one of a rollback's ``n`` slots.
    """
    return str(name).startswith("->")


def _real_undo_names(doc) -> list[str]:
    """Undo-stack names minus other documents' ghosts (see _is_ghost_entry)."""
    return [n for n in _undo_names(doc) if not _is_ghost_entry(n)]


def _stack_holds_journal(
    doc, records: list[sj.StepRecord], upto_index: int, undo_count: int
) -> bool:
    """Can the native undo be trusted to revert exactly these steps?

    The undo stack is shared with the GUI, and it does not survive a reopen:
    after one it holds NOTHING of the journal, while manual edits interleave on
    it in every session. Popping blind then undoes the WRONG transactions — and
    for a step that only changed properties no object moves, so the object-set
    verification in ``_rollback`` cannot tell. The stack's own names are the
    check: the entries about to be popped must be exactly the journal's
    transactions for these steps, most recent first (UndoNames[0] is the next
    entry undo() would take). Same-named entries are indistinguishable — every
    execute_code step commits as "CADPilot: execute_code" — but any foreign
    name in the way (or a too-short stack) downgrades to the rebuild path.
    """
    if undo_count <= 0:
        return True
    expected = [r.transaction for r in reversed(records) if r.index > upto_index and r.transaction]
    # Ghosts (other documents' transactions parked here) sit between this
    # document's entries; _stack_op pops straight past them, so the comparison
    # must look at the same non-ghost sequence or every rollback degraded to
    # the full rebuild while a model was merely sharing the FreeCAD instance.
    return _real_undo_names(doc)[:undo_count] == expected[:undo_count]


def _object_names(doc) -> list[str]:
    try:
        return sorted(o.Name for o in doc.Objects)
    except Exception:
        return []


def _transaction_name(rec: sj.StepRecord) -> str:
    name = rec.params.get("obj_name") or ""
    return f"CADPilot: {rec.operation} {name}".strip()


# --- engine-echo mute -----------------------------------------------------------
#
# The manual-edit observer (further down) mirrors property-panel edits into
# the journal. Undo/redo and step re-runs fire the SAME property-change
# notifications, but they write restored or already-recorded values: syncing
# those would undo a user's correction after the fact (the undo restores
# Height 6, the observer "manual-edits" the journal back to 6, the re-run
# rebuilds at 6 — the correction silently reverts). Every window where the
# ENGINE drives the document mutes the observer; a genuine GUI edit never
# happens inside one.

_ENGINE_ACTIVE = 0


class _EngineQuiet:
    """Context manager: while active, the manual-edit observer stays silent."""

    def __enter__(self) -> _EngineQuiet:
        global _ENGINE_ACTIVE
        _ENGINE_ACTIVE += 1
        return self

    def __exit__(self, *exc: object) -> bool:
        global _ENGINE_ACTIVE
        _ENGINE_ACTIVE -= 1
        return False


def engine_quiet() -> _EngineQuiet:
    """Mute the manual-edit observer for machine-driven document writes.

    The RPC layer wraps every mutation in this: a tool-driven change is
    recorded as its OWN step, and letting it also bleed into an earlier step's
    params through the observer made reject/replay inconsistent (reject the
    edit step and the earlier step still carries its value). Only human edits
    — the property panel, the sketcher, FreeCAD's Python console — may sync.
    """
    return _EngineQuiet()


@contextlib.contextmanager
def active_document(doc):
    """Make ``doc`` the App-active document while a transaction is recorded.

    FreeCAD 1.1.4 attributes an undo entry to the document that is ACTIVE at
    commit time, not to the one the transaction was opened on: committing on a
    non-active document leaves a ghost entry ("-> <name>") on the active
    document's stack, where a later ``undo()`` pops the ghost instead of its
    own step while this document's objects stay untouched — a rollback then
    reports success and truncates its log while the model never moved
    (live-verified, and reproduced with plain FreeCAD, no CADPilot involved).
    Activating the target for the open/commit window keeps every entry on its
    real stack; the previously active document is restored afterwards.
    """
    if doc is None:
        yield
        return
    previous = None
    with contextlib.suppress(Exception):
        active = FreeCAD.ActiveDocument
        previous = active.Name if active is not None else None
    if previous != doc.Name:
        with contextlib.suppress(Exception):
            FreeCAD.setActiveDocument(doc.Name)
    try:
        yield
    finally:
        if previous is not None and previous != doc.Name:
            with contextlib.suppress(Exception):
                FreeCAD.setActiveDocument(previous)


def undo_n(doc, n: int) -> dict[str, Any]:
    """Undo up to ``n`` transactions; report how many actually went.

    Never call undo() blind past the end of the stack (the raised exceptions
    are noise), and stop at the first failure so the reported count matches
    reality — both the session log and the journal are truncated by it.
    """
    return _stack_op(doc, n, undo=True)


def redo_n(doc, n: int) -> dict[str, Any]:
    return _stack_op(doc, n, undo=False)


def _stack_op(doc, n: int, undo: bool) -> dict[str, Any]:
    """Undo/redo up to ``n`` of THIS document's transactions.

    Ghost entries (``-> name``: another document's transaction that was
    committed while this one was active) are popped out of the way but do NOT
    consume a slot — they are no-ops here, and counting them is exactly how a
    rollback reported "undone N" while its own steps were still applied.
    """
    stack = [str(x) for x in (getattr(doc, "UndoNames" if undo else "RedoNames", []) or [])]
    if n <= 0 or not stack:
        return {"success": True, "count": 0, "ghosts_skipped": 0, "objects": _object_names(doc)}
    with _EngineQuiet():
        done = 0
        ghosts = 0
        idx = 0
        while done < n and idx < len(stack):
            ghost = _is_ghost_entry(stack[idx])
            idx += 1
            try:
                doc.undo() if undo else doc.redo()
            except Exception as e:
                FreeCAD.Console.PrintWarning(
                    f"CADPilot: {'undo' if undo else 'redo'} stopped: {e}\n"
                )
                break
            if ghost:
                ghosts += 1
            else:
                done += 1
        with contextlib.suppress(Exception):
            doc.recompute()
    if ghosts:
        dbglog.get_logger("tx").info(
            "stack op skipped %d ghost entry(ies) owned by other documents", ghosts
        )
    return {
        "success": True,
        "count": done,
        "ghosts_skipped": ghosts,
        "objects": _object_names(doc),
    }


# --- recording ---------------------------------------------------------------


def record_commit(
    doc,
    *,
    operation: str,
    label: str,
    params: dict[str, Any] | None = None,
    transaction: str = "",
    atomic: bool = True,
    executable: bool | None = None,
    objects_before: list[str] | None = None,
    objects_after: list[str] | None = None,
) -> sj.StepRecord | None:
    """Append a completed step.

    MUST be called inside the step's transaction, so the log entry commits with
    the model change (an abort must leave neither). Undo will not take it back
    out again — see the module docstring.
    """
    try:
        records = read_journal(doc)
        # A new commit invalidates the not-yet-executed plan: it was authored
        # against a document state that no longer exists. The plan is not
        # necessarily a trailing run (inspections append behind it, a snapshot
        # marker may sit there too), so the removal keys on STATE, not
        # position — slicing from planned_tail_start would remove nothing and
        # a stale plan would survive the commit.
        sj.drop_planned(records)
        rec = sj.StepRecord(
            index=len(records) + 1,
            state=sj.STATE_DONE,
            operation=operation,
            label=label or operation,
            params=dict(params or {}),
            transaction=transaction,
            atomic=atomic,
            executable=operation in EXECUTABLE_OPS if executable is None else executable,
            objects_before=list(objects_before or []),
            objects_after=list(objects_after or []),
            timestamp=sj.stamp(),
        )
        records.append(rec)
        write_journal(doc, records)
        return rec
    except Exception as e:
        FreeCAD.Console.PrintWarning(f"CADPilot: step journal write failed: {e}\n")
        return None


def object_names(doc) -> list[str]:
    """Public fingerprint of the document's object set (sorted names)."""
    return _object_names(doc)


def undo_token(doc) -> tuple:
    """FreeCAD's "did the last commit land" signal, without the undo-cap blind spot.

    ``UndoCount`` alone is NOT that signal: FreeCAD caps the undo stack (the
    ``MaxUndoSize`` preference — 20 by default), so once the stack is full a
    REAL commit leaves the count pinned. Using the count as the probe made the
    journal classify every mutation after the ~20th as read-only (no
    transaction, not re-executable), and rollback/replay then skipped real
    steps. Verified live on 1.1.4: at ``UndoCount == 20`` a real commit still
    moves ``UndoNames`` (oldest evicted, newest appended) while an EMPTY commit
    moves nothing — so the pair is decisive where the count alone is not.
    """
    return (int(getattr(doc, "UndoCount", 0)), tuple(getattr(doc, "UndoNames", None) or ()))


def document_tokens() -> dict[str, tuple]:
    """{document name: change token} for every open document.

    ``execute_code`` needs this: the snippet is free to switch documents (and
    to close one), so "which document did it change?" can only be answered by
    comparing every document before and after the run.
    """
    tokens: dict[str, tuple] = {}
    for doc in FreeCAD.listDocuments().values():
        with contextlib.suppress(Exception):
            tokens[doc.Name] = (*undo_token(doc), tuple(_object_names(doc)))
    return tokens


def changed_documents(before: dict[str, tuple]) -> list[str]:
    """Names of the documents whose token moved since ``before`` (sorted).

    A document that disappeared is not listed (there is nothing to reconcile);
    one that appeared counts as changed.
    """
    after = document_tokens()
    return sorted(name for name, token in after.items() if token != before.get(name))


def append_execute_code(
    doc,
    *,
    code: str,
    changed: bool,
    objects_before: list[str] | None = None,
) -> None:
    """Record an execute_code step.

    ``changed`` says whether the snippet produced an undo entry, i.e. whether it
    mutated the document inside the wrapper transaction the RPC handler opened
    around it:

    * changed -> ATOMIC and replayable. The code is stored in ``params`` so
      ``reexecute``/``replay`` can re-run it, and rollback treats it like any
      other transaction-bearing step.
    * unchanged -> a read-only inspection. Non-atomic (rollback must not stop at
      it) and non-executable (replay must not re-run it).
    """
    try:
        records = read_journal(doc)
        after = _object_names(doc)
        # A read-only execute_code — inspecting the model, the common case —
        # must NOT destroy a pending plan; only a call that actually moved the
        # object set invalidates it (see sj.invalidates_plan).
        if sj.invalidates_plan(records, after):
            sj.drop_planned(records)
        # The inspection is APPENDED at the very end, even behind a pending
        # plan. Inserting it before the tail shifted every planned step's
        # index, so an MCP client that read "pocket = step 11" and then called
        # reexecute(11) hit the inspection instead (live: JTest). A done
        # record behind the plan is harmless — the plan cursor walks by STATE
        # and run_to's upto check is cursor-based — while stable indices are
        # what index-addressed verbs (run_to/reexecute/rollback_to) run on.
        records.append(
            sj.StepRecord(
                index=len(records) + 1,
                state=sj.STATE_DONE,
                operation="execute_code",
                # A step row should say what the snippet DID — its first line is
                # usually `import FreeCAD` boilerplate. The full code lives in
                # params and is shown in the panel's detail pane.
                label=f"execute_code: {sj.effect_label(changed, list(objects_before or []), after)}",
                # The code is ALWAYS kept, read-only or not: the panel shows it
                # as the step's detail, and without it a row is an opaque "{}".
                params={"code": code},
                transaction="CADPilot: execute_code" if changed else "",
                atomic=changed,
                mutated=changed,
                executable=changed,
                objects_before=list(objects_before or []),
                objects_after=after,
                timestamp=sj.stamp(),
            )
        )
        write_journal(doc, records)  # reindexes: list position == step number
    except Exception as e:
        FreeCAD.Console.PrintWarning(f"CADPilot: step journal write failed: {e}\n")


# --- executing a record ------------------------------------------------------


def downgrade_if_no_undo(doc, rec: sj.StepRecord | None, token_before) -> bool:
    """Reconcile a just-committed step with the undo stack.

    An empty commit adds no undo entry — the op changed nothing (a read-only
    assembly ``verify``, an edit that set the values the object already had).
    A record that claims a transaction the stack does not have makes
    ``plan_rollback`` pop an EARLIER step's transaction, so the record is
    downgraded to read-only: no transaction, no mutation, no re-execution.

    ``token_before`` is :func:`undo_token` taken before the op; comparing it to
    the post-commit token is what makes this work at FreeCAD's undo cap, where
    the plain count stops moving (see :func:`undo_token`).
    """
    if rec is None:
        return False
    produced = True
    with contextlib.suppress(Exception):
        produced = undo_token(doc) != token_before
    if produced:
        return False
    records = read_journal(doc)
    target = next((r for r in records if r.index == rec.index), None)
    if target is None:
        return False
    target.atomic = False
    target.mutated = False
    target.executable = False
    target.transaction = ""
    with contextlib.suppress(Exception):
        write_journal(doc, records)
    logger.info(
        "step %d (%s): commit produced no undo entry, marked read-only",
        target.index,
        target.operation,
    )
    return True


def _normalize(res) -> dict[str, Any]:
    if res is True:
        return {"success": True}
    if isinstance(res, dict):
        return res
    return {"success": False, "error": str(res)}


def _replay_ready_code(doc, code: str) -> str:
    """Point a recorded snippet's document references at this document.

    The journal rides ON the document, but its snippets capture the document
    NAME they were written against. Save the file under a new name (or move it)
    and reopening hands FreeCAD a document named after the FILE — every re-run
    then dies with "Unknown document" before touching a shape, which silently
    broke rollback's rebuild path (live: DeskFan.FCStd, ex-MideaDeskFan, where
    every rebuild step failed on the rename). A reference that no longer
    resolves to an open document is rewritten to the host; one that DOES
    resolve is left alone, because the snippet may genuinely mean that other
    document.
    """
    if "getDocument" not in code:
        return code
    open_names = set(FreeCAD.listDocuments())
    stale = {
        name: doc.Name
        for name in sj.document_refs(code)
        if name != doc.Name and name not in open_names
    }
    if not stale:
        return code
    logger.warning(
        "journal replay: getDocument(%s) no longer resolves; rewrote to %r",
        ", ".join(repr(n) for n in sorted(stale)),
        doc.Name,
    )
    return sj.rewrite_document_refs(code, stale)


def _execute_one(doc, operation: str, params: dict[str, Any]) -> dict[str, Any]:
    """Run one non-batch operation against ``doc`` (no transaction, no journal)."""
    props = params.get("obj_properties") or {}

    if operation == "create_object":
        if not params.get("obj_type"):
            return {"success": False, "error": "missing obj_type"}
        return _normalize(
            create_object_gui(
                doc.Name,
                Object(
                    name=params.get("obj_name") or "New_Object",
                    type=params["obj_type"],
                    properties=props,
                ),
            )
        )
    if operation == "edit_object":
        return _normalize(
            edit_object_gui(doc.Name, Object(name=params.get("obj_name") or "", properties=props))
        )
    if operation == "delete_object":
        return _normalize(delete_object_gui(doc.Name, params.get("obj_name") or ""))
    if operation in FEATURE_TYPES:
        spec = {"type": operation, "base": params.get("obj_name"), **props}
        try:
            return {"success": True, "object_name": create_feature_gui(doc, spec).Name}
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
    if operation == "set_anchors":
        from rpc_server.assembly_ops import set_anchors

        return _normalize(
            set_anchors(
                doc.Name,
                str(params.get("obj_name") or ""),
                params.get("anchors") or {},
                bool(params.get("replace", False)),
                str(params.get("coord_frame") or "local"),
            )
        )
    if operation == "align_shapes":
        from rpc_server.geometry_query import align_shapes

        return _normalize(
            align_shapes(
                doc.Name,
                params.get("obj_name"),
                params.get("element"),
                params.get("element_index"),
                params.get("target_obj_name"),
                params.get("target_element"),
                params.get("target_element_index"),
                params.get("mode", "touch"),
                params.get("offset", 0.0),
            )
        )
    if operation == "assemble":
        from rpc_server.assembly_ops import assemble

        # Strict on re-run: the original call may have committed a PARTIAL
        # mate set (commit_if=passed>0), but replay must reproduce all of it —
        # a silently half-assembled rebuild is worse than a failed step.
        res = assemble(
            doc.Name,
            params.get("mates") or [],
            float(params.get("tolerance", 0.1)),
            bool(params.get("stop_on_error", True)),
        )
        if isinstance(res, dict) and res.get("success"):
            return {"success": True}
        return {
            "success": False,
            "error": (res.get("error") if isinstance(res, dict) else str(res)) or "assemble failed",
        }
    if operation == "assembly":
        spec = params.get("spec") or {}
        if not spec.get("operation"):
            return {"success": False, "error": "assembly step has no recorded spec"}
        from rpc_server.joint_ops import assembly_op

        try:
            res = assembly_op(doc, spec)
            return {
                "success": True,
                "object_name": str(
                    res.get("joint") or res.get("link") or res.get("assembly") or ""
                ),
            }
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
    if operation == "execute_code":
        # Re-run a recorded snippet (reexecute/replay). It runs inside the step's
        # own transaction (opened by run_record) and through the SAME executor
        # the RPC handler uses, so it sees the namespace it was written against.
        code = str(params.get("code") or "")
        if not code:
            return {"success": False, "error": "execute_code step has no recorded code"}
        code = _replay_ready_code(doc, code)
        from rpc_server import rpc_server as _rs

        try:
            _rs.exec_snippet(code)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
    return {"success": False, "error": f"operation '{operation}' is not re-executable"}


def execute_record(doc, rec: sj.StepRecord) -> dict[str, Any]:
    """Run one record's operation against ``doc`` (no transaction, no journal)."""
    if rec.operation == "batch":
        ops = (rec.params or {}).get("ops") or []
        if not ops:
            return {"success": False, "error": "batch step has no ops"}
        for i, sub in enumerate(ops, start=1):
            op_name = sj.sub_operation(sub)
            res = _execute_one(doc, op_name, sj.params_for(sub))
            if not res.get("success"):
                return {
                    "success": False,
                    "error": f"batch op {i} ({op_name}): {res.get('error')}",
                }
        return {"success": True}
    return _execute_one(doc, rec.operation, rec.params or {})


def run_record(doc, records: list[sj.StepRecord], rec: sj.StepRecord) -> dict[str, Any]:
    """Execute ``rec`` in its own transaction, journal written inside it."""
    with active_document(doc):
        return _run_record_locked(doc, records, rec)


def _run_record_locked(doc, records: list[sj.StepRecord], rec: sj.StepRecord) -> dict[str, Any]:
    tx = _transaction_name(rec)
    before = _object_names(doc)
    token_before = None
    with contextlib.suppress(Exception):
        token_before = undo_token(doc)
    doc.openTransaction(tx)
    logger.info("open transaction %r (%d objects)", tx, len(before))
    started = time.monotonic()
    with _EngineQuiet():
        try:
            res = execute_record(doc, rec)
        except Exception as e:
            res = {"success": False, "error": f"{type(e).__name__}: {e}"}
    rec.duration_ms = int((time.monotonic() - started) * 1000)

    if not res.get("success"):
        # abortTransaction restores the pre-step state: an undo in disguise,
        # and restoring writes the OLD values that the observer must not
        # mirror back over (possibly synced) params.
        with _EngineQuiet(), contextlib.suppress(Exception):
            doc.abortTransaction()
        rec.state = sj.STATE_FAILED
        rec.error = str(res.get("error") or "unknown error")
        logger.warning("aborted transaction %r: %s", tx, rec.error)
        # The transaction is gone, so this write is deliberately outside one.
        with contextlib.suppress(Exception):
            write_journal(doc, records)
        return res

    rec.state = sj.STATE_DONE
    rec.transaction = tx
    rec.objects_before = before
    rec.objects_after = _object_names(doc)
    rec.error = ""
    rec.result = str(res.get("object_name") or "")[:200]
    write_journal(doc, records)
    logger.info(
        "committed transaction %r (%d objects, %d journal records)",
        tx,
        len(rec.objects_after),
        len(records),
    )
    doc.commitTransaction()
    with _EngineQuiet(), contextlib.suppress(Exception):
        doc.recompute()
    # Self-correction for a re-run whose op ended up changing nothing (an
    # edited execute_code snippet, an edit that re-applied the same values):
    # the commit produced no undo entry, so the record must stop claiming one —
    # otherwise a later rollback would pop an EARLIER step's transaction off
    # the plain stack. (There is no path that upgrades in the other direction.)
    produced = True
    with contextlib.suppress(Exception):
        produced = undo_token(doc) != token_before
    if not produced and (rec.atomic or rec.transaction):
        rec.atomic = False
        rec.mutated = False
        rec.executable = False
        rec.transaction = ""
        logger.info("step %d (%s): re-run changed nothing, downgraded", rec.index, rec.operation)
        with contextlib.suppress(Exception):
            write_journal(doc, records)
    return res


# --- op dispatch -------------------------------------------------------------


def apply_op(doc, spec: dict[str, Any]) -> dict[str, Any]:
    """Single entry point for the panel and the RPC handler.

    Wraps :func:`_apply_op` so every journal operation — however it returns —
    lands in the log. The panel calls this straight from the GUI thread, so a
    button press that does nothing is otherwise invisible.

    Every mutating result carries a compact ``journal`` snapshot, so a caller
    (the MCP client especially) sees the resulting state in the same call and
    does not need a follow-up status round-trip.
    """
    operation = str(spec.get("operation") or "")
    started = time.monotonic()
    try:
        result = _apply_op(doc, spec)
    finally:
        # INFO: a panel button press is user-visible intent, so "what was
        # clicked and what came of it" belongs in the default trace.
        logger.info("journal %s finished in %.1fms", operation, (time.monotonic() - started) * 1000)
    if not result.get("success"):
        logger.warning("journal %s failed: %s", operation, result.get("error"))
    if operation != "status":
        with contextlib.suppress(Exception):
            result["journal"] = _compact_journal(doc)
    return result


def _compact_journal(doc) -> dict[str, Any]:
    """The steps list without params — small enough to attach to every reply."""
    records = read_journal(doc)
    names = _undo_names(doc)
    last = sj.last_atomic_done(records)
    return {
        "count": len(records),
        "done": sj.done_count(records),
        "planned": sj.pending_count(records),
        "accepted": sum(1 for r in records if r.accepted),
        "drift": bool(last and last.transaction not in names),
        "steps": [
            {
                "index": r.index,
                "state": r.state,
                "operation": r.operation,
                "label": r.label,
                "accepted": r.accepted,
                **({"error": r.error} if r.error else {}),
            }
            for r in records
        ],
    }


def _apply_op(doc, spec: dict[str, Any]) -> dict[str, Any]:
    operation = str(spec.get("operation") or "")
    records = read_journal(doc)

    if operation == "status":
        return _status(doc, records)
    if operation == "set_plan":
        added = sj.set_plan(records, list(spec.get("steps") or []), EXECUTABLE_OPS)
        meta = read_meta(doc)
        if spec.get("description"):
            meta["description"] = str(spec["description"])
        write_journal(doc, records, meta)
        return {"success": True, "planned": len(added), "count": len(records)}
    if operation == "clear_plan":
        sj.drop_planned(records)
        write_journal(doc, records)
        return {"success": True, "count": len(records)}
    if operation == "run_next":
        return _run_steps(doc, records, limit=1, upto=None)
    if operation == "run_all":
        return _run_steps(doc, records, limit=None, upto=None)
    if operation == "run_to":
        return _run_steps(doc, records, limit=None, upto=int(spec.get("index") or 0))
    if operation == "rollback_to":
        return _rollback(doc, records, int(spec.get("index") or 0), bool(spec.get("force")))
    if operation == "reexecute":
        return _reexecute(
            doc,
            records,
            int(spec.get("index") or 0),
            spec.get("params") or {},
            bool(spec.get("force")),
        )
    if operation == "accept":
        rec = sj.set_accepted(
            records, int(spec.get("index") or 0), bool((spec.get("params") or {}).get("on", True))
        )
        if rec is None:
            return {
                "success": False,
                "error": f"no done step {spec.get('index')} to accept/unaccept",
            }
        write_journal(doc, records)
        return {"success": True, "index": rec.index, "accepted": rec.accepted}
    if operation == "snapshot":
        p = spec.get("params") or {}
        return _snapshot(doc, records, str(p.get("note") or ""), bool(p.get("accept_done")))
    if operation == "reject":
        return _reject(
            doc,
            records,
            int(spec.get("index") or 0),
            bool(spec.get("force")),
            str((spec.get("params") or {}).get("reason") or ""),
        )
    if operation == "update":
        params = dict(spec.get("params") or {})
        label = str(params.pop("label", "") or "")
        rec = sj.update_planned(records, int(spec.get("index") or 0), params, label)
        if rec is None:
            return {
                "success": False,
                "error": f"step {spec.get('index')} is not planned/failed — done steps take reexecute",
            }
        write_journal(doc, records)
        return {"success": True, "index": rec.index}
    if operation == "insert":
        steps = list(spec.get("steps") or (spec.get("params") or {}).get("steps") or [])
        if not steps:
            return {
                "success": False,
                "error": "insert needs a non-empty steps list (a single step dict or "
                "{'steps': [...]}); the MCP step_control wraps params automatically",
            }
        added = sj.insert_steps(records, int(spec.get("index") or 0), steps, EXECUTABLE_OPS)
        if added is None:
            return {
                "success": False,
                "error": f"insert point {spec.get('index')} is inside executed history — "
                "the plan tail is append/insert-only",
            }
        write_journal(doc, records)
        return {"success": True, "inserted": [r.index for r in added]}
    if operation == "replay":
        from_index = int(spec.get("index") or 0)
        res = _rollback(doc, records, from_index, bool(spec.get("force")))
        if not res.get("success"):
            return res
        out = _run_steps(doc, read_journal(doc), limit=None, upto=None)
        out["replayed_from"] = from_index
        if not out.get("success"):
            # Replay rolls back to 0 FIRST, so a re-run failure leaves the
            # document holding only the steps that re-ran before it. The old
            # reply named the failing step and nothing else; the caller found an
            # emptied model on its next call (live: a failed batch step left 0
            # objects while the error text said nothing about it).
            out["document_objects"] = [o.Name for o in doc.Objects]
            out["warning"] = (
                "replay rolled the document back to step 0 before re-running, so it now "
                f"holds only what re-ran successfully ({len(out.get('executed', []))} "
                "step(s)); the model is NOT the pre-replay state. Fix the failing step "
                "(step_control update/reject) and replay again."
            )
        return out
    if operation == "reset":
        # confirm arrives top-level from the RPC spec and, for callers that
        # pass it in the generic params bag, nested under "params".
        confirm = bool(spec.get("confirm") or (spec.get("params") or {}).get("confirm"))
        if not confirm:
            return {"success": False, "error": "reset requires confirm=true"}
        write_journal(doc, [], meta={})
        return {"success": True, "count": 0}
    return {"success": False, "error": f"unknown journal operation '{operation}'"}


def _status(doc, records: list[sj.StepRecord]) -> dict[str, Any]:
    names = _undo_names(doc)
    # Anchor on the last ATOMIC step: a trailing non-atomic execute_code record
    # carries no transaction, so using the last completed record outright would
    # compare "" against the undo stack and always report clean.
    last = sj.last_atomic_done(records)
    # The last executed step's transaction should still be on the undo stack.
    # If it is not, work was undone (or redone past) behind the journal's back
    # — Ctrl+Z in the GUI — and counts derived from the log are suspect.
    drift = bool(last and last.transaction not in names)
    return {
        "success": True,
        "document": doc.Name,
        "count": len(records),
        "done": sj.done_count(records),
        "planned": sj.pending_count(records),
        "drift": drift,
        "meta": read_meta(doc),
        "records": [r.to_dict() for r in records],
    }


def _run_steps(doc, records, limit: int | None, upto: int | None) -> dict[str, Any]:
    executed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    while True:
        rec = sj.next_planned(records)
        if rec is None:
            break
        # The cursor decides the upto bound, never a done-count: done records
        # may legitimately sit BEHIND the plan (appended inspections, a
        # snapshot marker), and a done_count >= upto comparison then broke
        # early with planned steps still waiting.
        if upto is not None and rec.index > upto:
            break
        if limit is not None and len(executed) >= limit:
            break
        if not rec.executable:
            # execute_code & co. own no transaction and cannot be re-run, and a
            # read-only inspection is a normal journal citizen — run_all and
            # replay must not die on one. Mark it done (a rollback never undid
            # its effects, if any) and continue past it.
            rec.state = sj.STATE_DONE
            rec.error = ""
            rec.result = "skipped: not re-executable"
            skipped.append({"index": rec.index, "operation": rec.operation})
            logger.info("step %d (%s): skipped, not re-executable", rec.index, rec.operation)
            continue
        res = run_record(doc, records, rec)
        executed.append(
            {
                "index": rec.index,
                "operation": rec.operation,
                "success": bool(res.get("success")),
                "error": rec.error,
            }
        )
        if not res.get("success"):
            break
    if skipped:
        # Skips mutate no Shape, so there is no transaction to write inside of.
        write_journal(doc, records)
    return {
        "success": all(e["success"] for e in executed),
        "executed": executed,
        "skipped": skipped,
        "count": len(records),
        "done": sj.done_count(records),
        # Name the failing step: this text lands in the addon log and in MCP
        # replies, where a bare "ValueError: ..." is undebuggable.
        "error": next(
            (
                f"step {e['index']} ({e['operation']}): {e['error']}"
                for e in executed
                if not e["success"]
            ),
            "",
        ),
    }


def _snapshot(doc, records, note: str, accept_done: bool = False) -> dict[str, Any]:
    """Bookmark the current model state as the accepted baseline.

    For the "the user modeled outside the journal" flow: review the good steps,
    do the complex part by hand in the GUI, snapshot, and let the model continue
    from there over MCP. The marker is done + accepted, so rollback/replay
    refuses to cross it without force — that soft lock transitively protects
    the manual work, whose transactions the journal cannot count.
    objects_before (what the journal last knew) vs objects_after (the world
    now) names exactly what happened off-journal. The planned tail is KEPT: a
    snapshot is a marker, not a commit. ``accept_done`` accepts every done step
    in the same call, which bundles "everything so far is correct" into the
    baseline instead of ten separate accept clicks.
    """
    # Anchor on the last DONE record's snapshot — the journal's last known
    # state. The physical last record may be a planned tail entry, which never
    # ran and carries no snapshot; anchoring there would claim the world before
    # step one was empty and mark the user's whole document as "new".
    prev = next(
        (
            list(r.objects_after)
            for r in reversed(records)
            if r.state == sj.STATE_DONE and r.objects_after
        ),
        [],
    )
    accepted = 0
    if accept_done:
        for r in records:
            if r.state == sj.STATE_DONE and not r.accepted:
                r.accepted = True
                accepted += 1
    rec = sj.StepRecord(
        index=len(records) + 1,
        state=sj.STATE_DONE,
        operation="snapshot",
        label=note or "manual baseline",
        params={"note": note, "accept_done": accept_done},
        atomic=False,
        # A snapshot is a MARKER: it changes no geometry, so it must not claim a
        # mutation. `mutated=True` made it a rollback blocker (blocking =
        # not atomic AND mutated), which meant a force-less rollback across it
        # reported the non-atomic guard instead of the accepted soft-lock the
        # step exists to impose.
        mutated=False,
        executable=False,
        accepted=True,
        objects_before=prev,
        objects_after=_object_names(doc),
        timestamp=sj.stamp(),
    )
    records.append(rec)
    write_journal(doc, records)
    added = [n for n in rec.objects_after if n not in set(prev)]
    logger.info(
        "journal snapshot at step %d (%d new object(s), %d step(s) accepted)",
        rec.index,
        len(added),
        accepted,
    )
    return {"success": True, "index": rec.index, "added": added, "accepted": accepted}


def _remove_objects(doc, names: list[str]) -> list[str]:
    """Delete objects by name in one transaction; returns the names removed."""
    with active_document(doc):
        return _remove_objects_locked(doc, names)


def _remove_objects_locked(doc, names: list[str]) -> list[str]:
    removed: list[str] = []
    if not names:
        return removed
    present = set(_object_names(doc))
    targets = [n for n in names if n in present]
    if not targets:
        return removed
    doc.openTransaction("CADPilot: rollback cleanup")
    try:
        for name in targets:
            obj = doc.getObject(name)
            if obj is None:
                continue
            try:
                doc.removeObject(name)
                removed.append(name)
            except Exception as exc:  # still referenced, or not removable
                FreeCAD.Console.PrintWarning(f"CADPilot: could not remove '{name}': {exc}\n")
        doc.recompute()
        # Removing the feature that WAS a Body's Tip leaves the Body Invalid
        # until the tip is re-pointed; a rollback that drops a tip feature must
        # leave a usable document behind.
        if repair_body_tips(doc):
            doc.recompute()
        doc.commitTransaction()
    except Exception:
        with contextlib.suppress(Exception):
            doc.abortTransaction()
        raise
    return removed


def _rollback(doc, records, to_index: int, force: bool) -> dict[str, Any]:
    if to_index < 0 or to_index > len(records):
        return {"success": False, "error": f"step {to_index} out of range 0-{len(records)}"}
    plan = sj.plan_rollback(records, to_index)
    if plan["blocking"] and not force:
        return {
            "success": False,
            "error": (
                f"cannot roll back across non-atomic step(s) "
                f"{sj.blocking_text(records, plan['blocking'])}; pass force=true"
            ),
        }
    if plan["accepted"] and not force:
        return {
            "success": False,
            "error": (
                f"step(s) {plan['accepted']} are accepted (reviewed); "
                "pass force=true to roll back across them"
            ),
        }
    trust_undo = _stack_holds_journal(doc, records, to_index, plan["undo_count"])
    res = undo_n(doc, plan["undo_count"] if trust_undo else 0)
    # doc.undo() does NOT rewind the journal — the property is document-level and
    # FreeCAD only tracks undo for objects. This reconciliation is what makes
    # the log match the model again, so it runs on every rollback, not just as a
    # fallback.
    records = read_journal(doc)
    warnings: list[str] = []
    restored = "native"
    removed: list[str] = []
    # The count alone cannot certify the undo: the stack is shared with the GUI,
    # and a manual edit interleaved on it pops under this rollback's name while
    # the journal's own transaction stays applied. The object sets tell the
    # truth, so verify them before promising "native".
    present = set(_object_names(doc))
    leftover = sorted(set(sj.created_since(records, to_index)) & present)
    expected = set(sj.objects_after_index(records, to_index))
    journal_built = set(sj.created_since(records, 0))
    missing = sorted(n for n in expected if n not in present and n in journal_built)
    # Steps the undo stack cannot reach. Even when some transactions did come
    # off, these keep their changes, which is the "rollback reported success but
    # the objects are still there" bug.
    stranded = sj.steps_without_undo(records, to_index)

    triggers: list[str] = []
    if not trust_undo:
        triggers.append(
            "the undo stack does not hold the journal's transactions for these steps "
            "(the document was reopened, or edited outside the journal)"
        )
    elif res["count"] < plan["undo_count"]:
        triggers.append(
            f"the undo stack held only {res['count']}/{plan['undo_count']} of the "
            "journal's transactions"
        )
    if stranded:
        triggers.append(f"step(s) {stranded} own no undo entry")
    if leftover:
        triggers.append(
            f"object(s) {leftover} from the rolled-back steps are still present "
            "(off-journal edits on the undo stack?)"
        )
    if missing:
        triggers.append(f"object(s) {missing} that step {to_index} should have are gone")

    if triggers:
        reason = "; ".join(triggers)
        if not sj.unrecoverable_steps(records, to_index):
            # Everything up to the target can be re-created from the journal, so
            # rebuild: clear what the journal built, then run 1..to_index again.
            # This is a true restore, unlike a partial undo.
            removed = _remove_objects(doc, sj.created_since(records, 0))
            records = read_journal(doc)
            # Failed records belong in the re-run too: their transaction aborted,
            # so re-running them is safe, and skipping them would run a later
            # step against a missing dependency.
            for rec in records:
                if rec.state in (sj.STATE_DONE, sj.STATE_FAILED):
                    rec.state = sj.STATE_PLANNED
                    rec.transaction = ""
                    rec.error = ""
            write_journal(doc, records)
            run = _run_steps(doc, records, limit=None, upto=to_index)
            records = read_journal(doc)
            restored = "rebuild"
            warnings.append(
                f"{reason}. The model was rebuilt from the journal instead: "
                f"{len(removed)} object(s) removed and steps 1..{to_index} re-run."
            )
            stranded = []
            if not run.get("success"):
                return {
                    "success": False,
                    "error": f"the rebuild stopped early: {run.get('error') or 'unknown error'}",
                    "undone": res["count"],
                    "restored": restored,
                    "removed": removed,
                    "stranded": stranded,
                    "done": sj.done_count(records),
                    "count": len(records),
                    "warnings": warnings,
                }
        else:
            # Those steps can be neither undone nor re-run (their operation or
            # code was never recorded), so remove the objects they introduced.
            # Property changes they made cannot be restored.
            removal = sj.created_since(records, to_index)
            removed = _remove_objects(doc, removal)
            records = read_journal(doc)
            restored = "partial"
            warnings.append(
                f"{reason}. Their objects were removed ({len(removed)} object(s)), but "
                "property changes they made (placements, dimensions) cannot be restored. "
                "Replay the journal, or rebuild from scratch, if you need an exact state."
            )
            still = sorted(set(removal) & set(_object_names(doc)))
            if still:
                warnings.append(
                    f"object(s) {still} could not be removed and are still in the document"
                )

    extra = sj.done_count(records) - to_index
    if extra > 0:
        sj.rewind(records, extra)
        with contextlib.suppress(Exception):
            write_journal(doc, records)
    return {
        "success": True,
        "undone": res["count"],
        "restored": restored,
        "removed": removed,
        "stranded": stranded,
        "done": sj.done_count(records),
        "count": len(records),
        "warnings": warnings,
    }


def _reject(doc, records, index: int, force: bool, reason: str) -> dict[str, Any]:
    """Undo and forget everything from ``index`` onward (see sj.plan_reject).

    Reject is the deliberate act of destruction, so — unlike rollback/replay —
    it does not stop at accepted steps; it only refuses to cross non-atomic
    ones without force, where undo itself is unreliable.
    """
    plan = sj.plan_reject(records, index)
    if plan is None:
        return {"success": False, "error": f"no step {index}"}
    if plan["blocking"] and not force:
        return {
            "success": False,
            "error": (
                f"cannot reject across non-atomic step(s) "
                f"{sj.blocking_text(records, plan['blocking'])}; pass force=true"
            ),
        }
    trust_undo = _stack_holds_journal(doc, records, index - 1, plan["undo_count"])
    res = undo_n(doc, plan["undo_count"] if trust_undo else 0)
    records = read_journal(doc)
    warnings: list[str] = []
    if not trust_undo:
        warnings.append(
            "the undo stack does not hold the journal's transactions for these steps "
            "(the document was reopened, or edited outside the journal)"
        )
    # The same undo-stack problems as rollback: a rejected step with no undo
    # entry leaves its objects behind, and a manual edit interleaved on the
    # stack pops under this reject's name. The records are about to be DROPPED,
    # so there is no rebuild option — remove what the doomed steps introduced
    # (a no-op when the undo was clean), then say what could not be fixed.
    removal = sj.created_since(records, index - 1)
    removed = _remove_objects(doc, removal)
    records = read_journal(doc)
    present = set(_object_names(doc))
    still = sorted(set(removal) & present)
    if still:
        warnings.append(
            f"object(s) {still} from the rejected steps could not be removed and are "
            "still in the document"
        )
    kept_expected = set(sj.objects_after_index(records, index - 1))
    gone = sorted(
        n for n in kept_expected if n not in present and n in set(sj.created_since(records, 0))
    )
    if gone:
        warnings.append(
            f"object(s) {gone} that the kept steps created are missing (off-journal "
            "edits on the undo stack?); replay the journal to rebuild them"
        )
    if trust_undo and res["count"] < plan["undo_count"]:
        warnings.append(
            f"only {res['count']}/{plan['undo_count']} transactions could be undone "
            "(the FreeCAD undo stack was shorter than the journal)"
        )
    doomed = sj.steps_without_undo(records, index - 1)
    if doomed:
        warnings.append(
            f"rejected step(s) {doomed} owned no undo entry, so their objects were removed "
            f"directly ({len(removed)} object(s)); property changes they made cannot be "
            "restored"
        )
    del records[index - 1 :]
    write_journal(doc, records)
    logger.info(
        "rejected step %d onward (dropped %s)%s",
        index,
        plan["drop"],
        f": {reason}" if reason else "",
    )
    return {
        "success": True,
        "rejected": plan["drop"],
        "undone": res["count"],
        "removed": removed,
        "warnings": warnings,
    }


def _reexecute(
    doc, records, index: int, params: dict[str, Any], force: bool = False
) -> dict[str, Any]:
    rec = next((r for r in records if r.index == index), None)
    if rec is None:
        return {"success": False, "error": f"no step {index}"}
    if not rec.executable:
        return {"success": False, "error": f"step {index} ('{rec.operation}') is not re-executable"}
    plan = sj.plan_rollback(records, index - 1)
    if plan["blocking"]:
        return {
            "success": False,
            "error": (
                f"cannot re-run step {index}: non-atomic step(s) {plan['blocking']} sit in the way"
            ),
        }
    if plan["accepted"] and not force:
        return {
            "success": False,
            "error": (
                f"re-running step {index} rolls back across accepted step(s) "
                f"{plan['accepted']}; pass force=true"
            ),
        }
    trust_undo = _stack_holds_journal(doc, records, index - 1, plan["undo_count"])
    res = undo_n(doc, plan["undo_count"] if trust_undo else 0)
    records = read_journal(doc)
    # Re-running a step is only sound on a clean base: if the undo came up
    # short, popped the wrong transactions (off-journal edits on the stack), or
    # was skipped as untrustworthy, re-running would duplicate objects under
    # deduplicated names instead of failing. Verify the stack and object sets
    # first and point at rollback_to, which can rebuild, instead.
    if res["count"] < plan["undo_count"]:
        why = (
            "the undo stack does not match the journal (reopened document or off-journal edits)"
            if not trust_undo
            else "the undo stack is shorter than the journal"
        )
        return {
            "success": False,
            "error": (
                f"only {res['count']}/{plan['undo_count']} transactions could be undone "
                f"({why}); run rollback_to first, it can rebuild the model"
            ),
        }
    present = set(_object_names(doc))
    leftover = sorted(set(sj.created_since(records, index - 1)) & present)
    if leftover:
        return {
            "success": False,
            "error": (
                f"undo did not restore step {index - 1} cleanly: object(s) {leftover} from "
                "later steps are still present (off-journal edits on the undo stack?); "
                "run rollback_to first, it can rebuild the model"
            ),
        }
    rec = next((r for r in records if r.index == index), None)
    if rec is None:
        return {"success": False, "error": f"step {index} disappeared during rollback"}
    extra = sj.done_count(records) - (index - 1)
    if extra > 0:
        sj.rewind(records, extra)
    rec.state = sj.STATE_PLANNED
    rec.params = {**(rec.params or {}), **(params or {})}
    write_journal(doc, records)
    res = run_record(doc, records, rec)
    return {**res, "index": index, "count": len(records), "done": sj.done_count(records)}


# --- manual-edit sync ----------------------------------------------------------
#
# Human/machine collaboration needs the journal to tell the truth about the
# model: when the user corrects a dimension (or moves a part, edits a
# spreadsheet cell, rebinds an expression) in FreeCAD's GUI, a later
# reexecute/replay must not silently revert that correction. This observer
# mirrors GUI edits on objects a done step produced back into that step's
# params. What syncs: scalars the spec already carries (create/edit_object),
# the builder-known keys of every feature op (sj.FEATURE_SYNC — including
# ones left at default, or replay reverts the edit), spreadsheet cells and
# aliases (variables), dimensional constraint values and their expression
# bindings (sketch), datum/sketch attachment offsets, the Placement of
# create/edit steps, and the final pose of move-targeted objects (folded into
# the last move step as an absolute placement). Machine-driven writes never
# sync: engine windows (undo/redo/re-run) and every RPC-layer mutation run
# under _EngineQuiet — a tool's change is its own step, and echoing it into
# an earlier step's params made reject/replay inconsistent.

_SYNC_EVENTS: list[dict[str, Any]] = []


def pop_sync_events(doc_name: str) -> list[dict[str, Any]]:
    """Take (and clear) the queued manual-edit notices for one document."""
    mine = [e for e in _SYNC_EVENTS if e["doc"] == doc_name]
    if mine:
        _SYNC_EVENTS[:] = [e for e in _SYNC_EVENTS if e["doc"] != doc_name]
    return mine


def _num(value: float) -> int | float:
    f = float(value)
    return int(f) if f.is_integer() else f


def _json_scalar(value) -> int | float | str | bool | None:
    """A FreeCAD property value reduced to a JSON scalar, else None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return _num(value)
    if isinstance(value, str):
        return value
    q = getattr(value, "Value", None)  # Base.Quantity
    if isinstance(q, (int, float)):
        return _num(q)
    return None


def _same_scalar(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= 1e-9
    return a == b


# Feature-op property -> spec-key mapping lives in step_journal (pure,
# unit-tested): sj.FEATURE_SYNC, sj.tracked_objects, sj.map_cell_value,
# sj.map_constraint_value. The engine keeps only the FreeCAD reads.


def _uniform_edge_size(edges) -> int | float | None:
    """Uniform (index, start, end) edge-size list -> the single size, else None.

    The journal spec carries one scalar radius/size, so only a uniform edit
    maps back — per-edge sizes have no spec representation.
    """
    try:
        sizes = {round(float(t[1]), 9) for t in edges} | {round(float(t[2]), 9) for t in edges}
    except (TypeError, IndexError, ValueError):
        return None
    if len(sizes) == 1:
        return _num(sizes.pop())
    return None


def _placement_json(pl) -> dict:
    """FreeCAD.Placement -> the dict shape property_mapper accepts.

    Rotation.Angle reads back in RADIANS while the mapper constructs with
    DEGREES (FreeCAD.Rotation(axis, deg)) — convert here or a round-trip
    turns 90° into 1.57°.
    """
    rot = pl.Rotation
    return {
        "Base": {"x": _num(pl.Base.x), "y": _num(pl.Base.y), "z": _num(pl.Base.z)},
        "Rotation": {
            "Axis": {"x": _num(rot.Axis.x), "y": _num(rot.Axis.y), "z": _num(rot.Axis.z)},
            "Angle": _num(math.degrees(rot.Angle)),
        },
    }


def _flat_placement(d: dict) -> tuple:
    base = d.get("Base") or d.get("Position") or {}
    rot = d.get("Rotation") or {}
    axis = rot.get("Axis") or {}
    return (
        float(base.get("x", 0)),
        float(base.get("y", 0)),
        float(base.get("z", 0)),
        float(axis.get("x", 0)),
        float(axis.get("y", 0)),
        float(axis.get("z", 1)),
        float(rot.get("Angle", 0)),
    )


def _same_placement(a, b) -> bool:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    fa, fb = _flat_placement(a), _flat_placement(b)
    return all(abs(x - y) <= 1e-9 for x, y in zip(fa, fb, strict=True))


def _placement_brief(d) -> str:
    if not isinstance(d, dict):
        return "-"
    base = d.get("Base") or {}
    rot = d.get("Rotation") or {}
    axis = rot.get("Axis") or {}
    pos = ",".join(str(_num(base.get(k, 0))) for k in "xyz")
    ax = ",".join(str(_num(axis.get(k, 0))) for k in "xyz")
    return f"pos({pos}) rot {_num(rot.get('Angle', 0))}deg@({ax})"


def _sync_props_target(rec: sj.StepRecord, sub: int | None) -> dict[str, Any] | None:
    """The obj_properties dict a manual-edit sync write lands in.

    ``sub`` routes the write into a batch record's sub-op (tracked_objects
    claims create/edit/move sub-ops); None is a top-level step.
    """
    if sub is None:
        return rec.params.setdefault("obj_properties", {})
    ops = (rec.params or {}).get("ops") or []
    if isinstance(sub, int) and 0 <= sub < len(ops) and isinstance(ops[sub], dict):
        return ops[sub].setdefault("obj_properties", {})
    return None


class _JournalSyncObserver:
    def __init__(self):
        self._cache: dict[str, tuple[str, dict]] = {}
        self._writing = False

    def slotChangedObject(self, obj, prop):
        # _writing guards the observer's own journal write; _ENGINE_ACTIVE
        # mutes machine-driven windows (engine undo/redo/re-run, and every
        # RPC-layer mutation — a tool's change is its own step, and letting it
        # bleed into an earlier step's params made reject/replay inconsistent).
        if self._writing or _ENGINE_ACTIVE:
            return
        # An observer must never break the host's edit.
        with contextlib.suppress(Exception):
            self._sync(obj, prop)

    def _sync(self, obj, prop) -> None:
        doc = getattr(obj, "Document", None)
        if doc is None:
            return
        text = getattr(doc, sj.JOURNAL_PROP, "") or ""
        if not text:
            return
        cached = self._cache.get(doc.Name)
        if cached is None or cached[0] != text:
            cached = (text, sj.tracked_objects(sj.from_json(text)))
            self._cache[doc.Name] = cached
        entry = cached[1].get(getattr(obj, "Name", ""))
        if entry is None:
            return
        # The value compare inside each handler makes repeat firings (a cell
        # edit fires both 'cells' and the address) no-ops, so trigger wide.
        if entry.get("sheet") is not None:
            self._sync_cells(obj, doc, sj.from_json(text), entry["sheet"])
            return
        if entry.get("sketch") is not None and prop == "Constraints":
            self._sync_constraints(obj, doc, sj.from_json(text), entry["sketch"])
            return
        hit = entry["props"].get(prop)
        if hit is None:
            # A move-owned object's pose belongs to its LAST move step.
            if prop == "Placement" and entry.get("move") is not None:
                self._sync_move_fold(obj, doc, sj.from_json(text), *entry["move"])
            return
        records = sj.from_json(text)
        rec = next((r for r in records if r.index == hit[0]), None)
        if rec is None:
            return
        if prop == "Placement":
            value, same, brief = (
                _placement_json(obj.Placement),
                _same_placement,
                _placement_brief,
            )
        elif prop == "Edges":
            value = _uniform_edge_size(getattr(obj, prop, None))
            if value is None:
                return
            same, brief = _same_scalar, repr
        elif prop == "AttachmentOffset":
            # Only a pure-z translation maps onto the spec's scalar offset; a
            # rotated/shifted attachment has no spec representation.
            pl = getattr(obj, prop, None)
            base, rot = getattr(pl, "Base", None), getattr(pl, "Rotation", None)
            if base is None or rot is None:
                return
            if abs(base.x) > 1e-9 or abs(base.y) > 1e-9 or abs(rot.Angle) > 1e-9:
                return
            value, same, brief = _num(base.z), _same_scalar, repr
        else:
            # An expression binding wins over the literal value, in both
            # directions: binding in the GUI stores "=expr" (the builder
            # re-binds it on re-run), unbinding stores the number it fell
            # back to.
            exprs = dict(getattr(obj, "ExpressionEngine", None) or [])
            expr = exprs.get(prop)
            if expr:
                value = "=" + "".join(str(expr).split())
            else:
                value = _json_scalar(getattr(obj, prop, None))
                if value is None:
                    return
            same, brief = _same_scalar, repr
        props = _sync_props_target(rec, hit[2])
        if props is None:
            return
        current = props.get(hit[1])
        if same(current, value):
            return
        props[hit[1]] = value
        self._commit_sync(
            doc, records, rec.index, f"{obj.Name}.{prop}", brief(current), brief(value)
        )

    def _sync_cells(self, obj, doc, records, index: int) -> None:
        """Mirror spreadsheet cell edits (values, formulas, aliases) into the
        owning variables step's ``cells`` spec."""
        rec = next((r for r in records if r.index == index), None)
        if rec is None:
            return
        cells = (rec.params.get("obj_properties") or {}).get("cells")
        if not isinstance(cells, dict) or not cells:
            return
        changed = 0
        for cell, cell_entry in cells.items():
            if not (isinstance(cell_entry, list) and len(cell_entry) == 2):
                continue
            alias, old = cell_entry
            try:
                live_contents = obj.getContents(cell)
            except Exception:
                continue  # the cell was deleted by hand — leave the spec as-is
            new = sj.map_cell_value(old, live_contents)
            if new is not sj.UNREADABLE and not _same_scalar(old, new):
                cell_entry[1] = new
                changed += 1
            try:
                live_alias = obj.getAlias(cell) or ""
            except Exception:
                live_alias = alias
            if live_alias and live_alias != alias:
                cell_entry[0] = live_alias
                changed += 1
        if not changed:
            return
        self._commit_sync(doc, records, index, f"{obj.Name}.cells", "-", f"{changed} cell field(s)")

    def _sync_constraints(self, obj, doc, records, index: int) -> None:
        """Mirror dimensional-constraint edits into the owning sketch step.

        The spec's constraint list maps to the live sketch BY INDEX, so the
        whole sketch is skipped the moment the sequences diverge (a constraint
        added or removed by hand shifts every later index — there is no safe
        partial mapping).
        """
        rec = next((r for r in records if r.index == index), None)
        if rec is None:
            return
        pcons = (rec.params.get("obj_properties") or {}).get("constraints")
        if not isinstance(pcons, list) or not pcons:
            return
        live = list(getattr(obj, "Constraints", None) or [])
        if len(live) != len(pcons):
            return
        exprs = dict(getattr(obj, "ExpressionEngine", None) or [])
        updates = []
        for i, pc in enumerate(pcons):
            if not isinstance(pc, dict):
                return
            ctype = str(pc.get("type") or "")
            if sj.CONSTRAINT_TYPE_MAP.get(ctype) != getattr(live[i], "Type", None):
                return
            if ctype not in sj.DIMENSIONAL_CONSTRAINTS or "value" not in pc:
                continue
            expr = exprs.get(f"Constraints[{i}]")
            new = sj.map_constraint_value(
                ctype, pc.get("value"), getattr(live[i], "Value", None), expr
            )
            if new is sj.UNREADABLE:
                continue
            if not _same_scalar(pc.get("value"), new):
                updates.append((i, new))
        if not updates:
            return
        for i, new in updates:
            pcons[i]["value"] = new
        self._commit_sync(
            doc, records, index, f"{obj.Name}.Constraints", "-", f"{len(updates)} value(s)"
        )

    def _sync_move_fold(self, obj, doc, records, index: int, sub: int | None) -> None:
        """Fold a manual drag of a move-owned object into its last move step.

        A move is RELATIVE, so the drag cannot be expressed against it — the
        owning obj_properties become an absolute placement override instead
        (the builder's placement wins over translate/rotate on re-run), which
        reproduces the current pose no matter what came before the move.
        ``sub`` routes the fold into a batch record's move sub-op.
        """
        rec = next((r for r in records if r.index == index), None)
        if rec is None:
            return
        if sub is None:
            if rec.operation != "move":
                return
            parent = rec.params
        else:
            ops = (rec.params or {}).get("ops") or []
            if not (isinstance(sub, int) and 0 <= sub < len(ops) and isinstance(ops[sub], dict)):
                return
            if sj.sub_operation(ops[sub]) != "move":
                return
            parent = ops[sub]
        value = _placement_json(obj.Placement)
        current = (parent.get("obj_properties") or {}).get("placement")
        if _same_placement(current, value):
            return
        parent["obj_properties"] = {"placement": value}
        self._commit_sync(
            doc,
            records,
            index,
            f"{obj.Name}.Placement",
            _placement_brief(current),
            _placement_brief(value),
        )

    def _commit_sync(self, doc, records, index: int, what: str, old: str, new: str) -> None:
        self._writing = True
        try:
            write_journal(doc, records)
        finally:
            self._writing = False
        logger.info("journal sync: step %d %s = %s (manual edit)", index, what, new)
        _SYNC_EVENTS.append({"doc": doc.Name, "index": index, "prop": what, "old": old, "new": new})
        del _SYNC_EVENTS[:-50]


_OBSERVER_ATTR = "_cadpilot_journal_sync"


def install_sync_observer() -> None:
    """(Re)install the singleton observer; safe across hot reloads."""
    old = getattr(FreeCAD, _OBSERVER_ATTR, None)
    if old is not None:
        with contextlib.suppress(Exception):
            FreeCAD.removeDocumentObserver(old)
    observer = _JournalSyncObserver()
    FreeCAD.addDocumentObserver(observer)
    setattr(FreeCAD, _OBSERVER_ATTR, observer)


install_sync_observer()
