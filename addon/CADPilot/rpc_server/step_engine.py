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
)
from rpc_server.property_mapper import Object

logger = dbglog.get_logger("journal")

EXECUTABLE_OPS = {"create_object", "edit_object", "delete_object", "batch"} | set(FEATURE_TYPES)


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


def _object_names(doc) -> list[str]:
    try:
        return sorted(o.Name for o in doc.Objects)
    except Exception:
        return []


def _transaction_name(rec: sj.StepRecord) -> str:
    name = rec.params.get("obj_name") or ""
    return f"CADPilot: {rec.operation} {name}".strip()


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
    if n <= 0:
        return {"success": True, "count": 0, "objects": _object_names(doc)}
    stack = list(getattr(doc, "UndoNames" if undo else "RedoNames", []) or [])
    done = 0
    for _ in range(min(n, len(stack))):
        try:
            doc.undo() if undo else doc.redo()
            done += 1
        except Exception as e:
            FreeCAD.Console.PrintWarning(f"CADPilot: {'undo' if undo else 'redo'} stopped: {e}\n")
            break
    with contextlib.suppress(Exception):
        doc.recompute()
    return {"success": True, "count": done, "objects": _object_names(doc)}


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
        # A new commit invalidates the not-yet-executed tail: it was planned
        # against a document state that no longer exists.
        del records[sj.planned_tail_start(records) :]
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


def append_non_atomic(doc, *, label: str) -> None:
    """Record an execute_code step: no transaction, so rollback past it is unsafe."""
    try:
        records = read_journal(doc)
        after = _object_names(doc)
        # A read-only execute_code — inspecting the model, the common case —
        # must NOT destroy a pending plan; only a call that actually moved the
        # object set invalidates the tail (see sj.invalidates_plan).
        if sj.invalidates_plan(records, after):
            del records[sj.planned_tail_start(records) :]
        records.append(
            sj.StepRecord(
                index=len(records) + 1,
                state=sj.STATE_DONE,
                operation="execute_code",
                # A snippet is multi-line; the panel shows one row per step, so
                # collapse the whitespace before it becomes the row label.
                label=" ".join(str(label).split())[:80],
                params={},
                atomic=False,
                executable=False,
                objects_after=after,
                timestamp=sj.stamp(),
            )
        )
        write_journal(doc, records)
    except Exception as e:
        FreeCAD.Console.PrintWarning(f"CADPilot: step journal write failed: {e}\n")


# --- executing a record ------------------------------------------------------


def _normalize(res) -> dict[str, Any]:
    if res is True:
        return {"success": True}
    if isinstance(res, dict):
        return res
    return {"success": False, "error": str(res)}


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
    tx = _transaction_name(rec)
    before = _object_names(doc)
    doc.openTransaction(tx)
    logger.info("open transaction %r (%d objects)", tx, len(before))
    started = time.monotonic()
    try:
        res = execute_record(doc, rec)
    except Exception as e:
        res = {"success": False, "error": f"{type(e).__name__}: {e}"}
    rec.duration_ms = int((time.monotonic() - started) * 1000)

    if not res.get("success"):
        with contextlib.suppress(Exception):
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
    with contextlib.suppress(Exception):
        doc.recompute()
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
        del records[sj.planned_tail_start(records) :]
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
        return _snapshot(doc, records, str((spec.get("params") or {}).get("note") or ""))
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
        added = sj.insert_steps(records, int(spec.get("index") or 0), steps, EXECUTABLE_OPS)
        if added is None:
            return {
                "success": False,
                "error": "insert point is inside executed history (or steps empty) — "
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
    while True:
        if upto is not None and sj.done_count(records) >= upto:
            break
        if limit is not None and len(executed) >= limit:
            break
        rec = sj.next_planned(records)
        if rec is None:
            break
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
    return {
        "success": all(e["success"] for e in executed),
        "executed": executed,
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


def _snapshot(doc, records, note: str) -> dict[str, Any]:
    """Bookmark the current model state as the accepted baseline.

    For the "the user modeled outside the journal" flow: the marker is done +
    accepted, so rollback/replay refuses to cross it without force — that
    soft-lock transitively protects the manual work, whose transactions the
    journal cannot count. objects_before (what the journal last knew) vs
    objects_after (the world now) names exactly what happened off-journal.
    The planned tail is KEPT: a snapshot is a marker, not a commit.
    """
    prev = list(records[-1].objects_after) if records else []
    rec = sj.StepRecord(
        index=len(records) + 1,
        state=sj.STATE_DONE,
        operation="snapshot",
        label=note or "manual baseline",
        params={"note": note},
        atomic=False,
        executable=False,
        accepted=True,
        objects_before=prev,
        objects_after=_object_names(doc),
        timestamp=sj.stamp(),
    )
    records.append(rec)
    write_journal(doc, records)
    added = [n for n in rec.objects_after if n not in set(prev)]
    logger.info("journal snapshot at step %d (%d new object(s))", rec.index, len(added))
    return {"success": True, "index": rec.index, "added": added}


def _rollback(doc, records, to_index: int, force: bool) -> dict[str, Any]:
    if to_index < 0 or to_index > len(records):
        return {"success": False, "error": f"step {to_index} out of range 0-{len(records)}"}
    plan = sj.plan_rollback(records, to_index)
    if plan["blocking"] and not force:
        return {
            "success": False,
            "error": (
                f"cannot roll back across non-atomic step(s) {plan['blocking']} "
                "(execute_code without a transaction); pass force=true"
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
    res = undo_n(doc, plan["undo_count"])
    # doc.undo() does NOT rewind the journal — the property is document-level and
    # FreeCAD only tracks undo for objects. This reconciliation is what makes
    # the log match the model again, so it runs on every rollback, not just as a
    # fallback.
    records = read_journal(doc)
    extra = sj.done_count(records) - to_index
    if extra > 0:
        sj.rewind(records, extra)
        with contextlib.suppress(Exception):
            write_journal(doc, records)
    warnings = []
    if res["count"] < plan["undo_count"]:
        warnings.append(
            f"only {res['count']}/{plan['undo_count']} transactions could be undone "
            "(the FreeCAD undo stack was shorter than the journal)"
        )
    return {
        "success": True,
        "undone": res["count"],
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
                f"cannot reject across non-atomic step(s) {plan['blocking']} "
                "(execute_code without a transaction); pass force=true"
            ),
        }
    res = undo_n(doc, plan["undo_count"])
    records = read_journal(doc)
    del records[index - 1 :]
    write_journal(doc, records)
    logger.info(
        "rejected step %d onward (dropped %s)%s",
        index,
        plan["drop"],
        f": {reason}" if reason else "",
    )
    warnings = []
    if res["count"] < plan["undo_count"]:
        warnings.append(
            f"only {res['count']}/{plan['undo_count']} transactions could be undone "
            "(the FreeCAD undo stack was shorter than the journal)"
        )
    return {"success": True, "rejected": plan["drop"], "undone": res["count"], "warnings": warnings}


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
    undo_n(doc, plan["undo_count"])
    records = read_journal(doc)
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
# model: when the user corrects a dimension (or moves/rotates a part) in
# FreeCAD's property panel, a later reexecute/replay must not silently revert
# that correction. This observer mirrors GUI edits on objects a done step
# produced back into that step's ``params["obj_properties"]``. Scope is
# deliberately narrow: only scalar properties ALREADY present in the params
# are synced (the spec's structure is never invented), plus the Placement of
# create_object/edit_object steps (a reexecute re-applies obj_properties and
# would otherwise teleport the part back to the origin). Engine ops never
# echo here — re-creation writes the same values the params already hold,
# which the diff check swallows.

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


# Feature ops whose spec keys map onto a feature-object property (verified
# against the builders in feature_ops.py): object property ->
# params["obj_properties"] key. Richer values (edge selectors, links,
# boolean tool compounds) are intentionally not synced. FreeCAD >= 1.1 moved
# fillet/chamfer sizes off the scalar Radius/Size property into per-edge
# Edges tuples, so both names are claimed (only the one that exists fires).
_FEATURE_SYNC: dict[str, dict[str, str]] = {
    "fillet": {"Radius": "radius", "Edges": "radius"},
    "chamfer": {"Size": "size", "Edges": "size"},
    "pad": {"Length": "length"},
    "pocket": {"Length": "length"},
    "revolution": {"Angle": "angle"},
    "groove": {"Angle": "angle"},
    "thickness": {"Value": "value"},
    "draft": {"Angle": "angle"},
}


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


def _tracked_objects(records) -> dict[str, dict[str, tuple[int, str]]]:
    """object name -> {object property -> (step index, params key)}.

    Later done steps win per property, so a sync lands on the step that last
    decided that property. Placement is claimed by create_object/edit_object
    unless a ``move`` step targets the object: a move is a RELATIVE change,
    so it must keep owning the final pose (an absolute create-time Placement
    plus the relative move would double-apply on reexecute).
    """
    moved = {str(r.params.get("obj_name") or "") for r in records if r.operation == "move"}
    tracked: dict[str, dict[str, tuple[int, str]]] = {}
    for rec in records:
        if rec.state != sj.STATE_DONE:
            continue
        props = rec.params.get("obj_properties") or {}
        if rec.operation in ("create_object", "edit_object"):
            claims = {k: k for k, v in props.items() if isinstance(v, (int, float, str, bool))}
            if rec.operation == "create_object":
                names = set(rec.objects_after) - set(rec.objects_before)
            else:
                names = {str(rec.params.get("obj_name") or "")}
        else:
            claims = {
                prop: key
                for prop, key in _FEATURE_SYNC.get(rec.operation, {}).items()
                if key in props
            }
            names = set(rec.objects_after) - set(rec.objects_before)
        for name in names:
            if not name:
                continue
            entry = tracked.setdefault(name, {})
            for prop, key in claims.items():
                entry[prop] = (rec.index, key)
            if rec.operation in ("create_object", "edit_object") and name not in moved:
                entry["Placement"] = (rec.index, "Placement")
    return tracked


class _JournalSyncObserver:
    def __init__(self):
        self._cache: dict[str, tuple[str, dict]] = {}
        self._writing = False

    def slotChangedObject(self, obj, prop):
        if self._writing:
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
            cached = (text, _tracked_objects(sj.from_json(text)))
            self._cache[doc.Name] = cached
        hit = cached[1].get(getattr(obj, "Name", ""), {}).get(prop)
        if hit is None:
            return
        if prop == "Placement":
            value, same, brief = _placement_json(obj.Placement), _same_placement, _placement_brief
        elif prop == "Edges":
            value = _uniform_edge_size(getattr(obj, prop, None))
            if value is None:
                return
            same, brief = _same_scalar, repr
        else:
            value = _json_scalar(getattr(obj, prop, None))
            if value is None:
                return
            same, brief = _same_scalar, repr
        records = sj.from_json(text)
        rec = next((r for r in records if r.index == hit[0]), None)
        if rec is None:
            return
        current = (rec.params.get("obj_properties") or {}).get(hit[1])
        if same(current, value):
            return
        rec.params["obj_properties"][hit[1]] = value
        self._writing = True
        try:
            write_journal(doc, records)
        finally:
            self._writing = False
        logger.info(
            "journal sync: step %d %s.%s = %s (manual edit)",
            rec.index,
            obj.Name,
            prop,
            brief(value),
        )
        _SYNC_EVENTS.append(
            {
                "doc": doc.Name,
                "index": rec.index,
                "prop": prop,
                "old": brief(current),
                "new": brief(value),
            }
        )
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
