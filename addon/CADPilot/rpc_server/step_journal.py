"""Step journal — the per-document record behind the steps panel.

Pure data model and arithmetic: NO FreeCAD imports, so it is unit-testable
from ``tests/`` without a running FreeCAD. The FreeCAD-facing half (document
property persistence, step execution, undo) lives in ``step_engine.py``.

Why a journal at all: the MCP-side modeling session (``session_state.py``)
logs steps for the LLM, but it lives in the MCP process and only exists while
a session is active. The panel must work in FreeCAD's own process, with the
RPC server stopped, so the addon keeps its own copy on the document.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

JOURNAL_PROP = "MCP_StepJournal"
JOURNAL_VERSION = 1

STATE_PLANNED = "planned"
STATE_DONE = "done"
STATE_FAILED = "failed"


@dataclass
class StepRecord:
    """One step: either planned (not yet applied) or executed."""

    index: int
    state: str = STATE_DONE
    operation: str = ""
    label: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    transaction: str = ""
    atomic: bool = True
    # mutated=False: the step provably changed nothing (a read-only execute_code,
    # confirmed by the absence of an undo entry). It owns no transaction, so it
    # cannot make undo revert the wrong change — it neither blocks a rollback nor
    # counts for undo_count, and replay skips it.
    mutated: bool = True
    executable: bool = True
    objects_before: list[str] = field(default_factory=list)
    objects_after: list[str] = field(default_factory=list)
    error: str = ""
    timestamp: str = ""
    accepted: bool = False
    note: str = ""
    result: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StepRecord:
        return cls(
            index=int(data.get("index", 0)),
            state=data.get("state", STATE_DONE),
            operation=data.get("operation", ""),
            label=data.get("label", ""),
            params=dict(data.get("params") or {}),
            transaction=data.get("transaction", ""),
            atomic=bool(data.get("atomic", True)),
            mutated=bool(data.get("mutated", True)),
            executable=bool(data.get("executable", True)),
            objects_before=list(data.get("objects_before") or []),
            objects_after=list(data.get("objects_after") or []),
            error=data.get("error", ""),
            timestamp=data.get("timestamp", ""),
            accepted=bool(data.get("accepted", False)),
            note=data.get("note", ""),
            result=data.get("result", ""),
            duration_ms=int(data.get("duration_ms", 0)),
        )


def stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def to_json(records: list[StepRecord], meta: dict[str, Any] | None = None) -> str:
    return json.dumps(
        {
            "version": JOURNAL_VERSION,
            "meta": dict(meta or {}),
            "records": [r.to_dict() for r in records],
        },
        ensure_ascii=False,
    )


def meta_from_json(text: str | None) -> dict[str, Any]:
    """The journal-level meta block (plan description); never raises."""
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    meta = data.get("meta")
    return dict(meta) if isinstance(meta, dict) else {}


def from_json(text: str | None) -> list[StepRecord]:
    """Parse the journal property; never raises (a corrupt journal is empty)."""
    if not text:
        return []
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    out: list[StepRecord] = []
    for raw in data.get("records") or []:
        if not isinstance(raw, dict):
            continue
        try:
            out.append(StepRecord.from_dict(raw))
        except (TypeError, ValueError):
            continue
    return out


def reindex(records: list[StepRecord]) -> None:
    for i, rec in enumerate(records, start=1):
        rec.index = i


def describe_step(step: dict[str, Any]) -> str:
    op = str(step.get("operation", ""))
    name = step.get("obj_name") or ""
    if op == "batch":
        return f"batch ({len(step.get('ops') or [])} ops)"
    return f"{op} '{name}'" if name else op


def params_for(step: dict[str, Any]) -> dict[str, Any]:
    """The exact payload ``step_engine.execute_record`` needs to re-run a step."""
    return {
        "obj_name": step.get("obj_name"),
        "obj_type": step.get("obj_type"),
        "obj_properties": step.get("obj_properties") or {},
        "ops": step.get("ops") or [],
    }


def sub_operation(sub: dict[str, Any]) -> str:
    """A batch sub-op's operation name, tolerating both key conventions.

    cad() batch ops arrive as {"action": ...} (the RPC batch schema) and are
    journaled verbatim, while journal-native steps use {"operation": ...} —
    without this, re-running a recorded batch step resolves "" and dies with
    "operation '' is not re-executable".
    """
    return str(sub.get("operation") or sub.get("action") or "")


def blocking_text(records: list[StepRecord], indices: list[int]) -> str:
    """Name the steps a rollback must be forced across, with their real ops.

    The message used to hardcode "execute_code", but a snapshot marker is also
    a blocking, non-transactional record — naming the wrong op sends the user
    looking in the wrong place.
    """
    ops: list[str] = []
    for index in indices:
        rec = next((r for r in records if r.index == index), None)
        op = rec.operation if rec is not None else "?"
        if op not in ops:
            ops.append(op)
    return f"{indices} ({'/'.join(ops)} without a transaction)"


def effect_label(changed: bool, before: list[str], after: list[str]) -> str:
    """Describe what a snippet DID — what an execute_code step row should say.

    The code's first line is usually boilerplate (``import FreeCAD`` /
    ``doc = FreeCAD.getDocument(...)``), so the object delta is the honest
    summary: "read-only", "+4 object(s): Hole1, …", or "changed properties" for
    a snippet that only edited existing ones.
    """
    if not changed:
        return "read-only"
    added = [n for n in after if n not in set(before)]
    removed = [n for n in before if n not in set(after)]
    if added:
        head = ", ".join(added[:3]) + (", …" if len(added) > 3 else "")
        return f"+{len(added)} object(s): {head}"
    if removed:
        return f"-{len(removed)} object(s)"
    return "changed properties"


def build_record(
    step: dict[str, Any],
    index: int,
    state: str,
    executable_ops: set[str],
    label: str = "",
) -> StepRecord:
    op = str(step.get("operation", ""))
    return StepRecord(
        index=index,
        state=state,
        operation=op,
        label=label or step.get("description") or describe_step(step),
        params=params_for(step),
        executable=op in executable_ops,
        timestamp=stamp(),
    )


def planned_tail_start(records: list[StepRecord]) -> int:
    """Index of the first record in the trailing run of ``planned`` records."""
    n = len(records)
    while n > 0 and records[n - 1].state == STATE_PLANNED:
        n -= 1
    return n


def set_plan(
    records: list[StepRecord], steps: list[dict[str, Any]], executable_ops: set[str]
) -> list[StepRecord]:
    """Replace the not-yet-executed tail with a fresh plan."""
    del records[planned_tail_start(records) :]
    added: list[StepRecord] = []
    for step in steps:
        rec = build_record(step, len(records) + 1, STATE_PLANNED, executable_ops)
        records.append(rec)
        added.append(rec)
    reindex(records)
    return added


def next_planned(records: list[StepRecord]) -> StepRecord | None:
    for rec in records:
        if rec.state == STATE_PLANNED:
            return rec
    return None


def pending_count(records: list[StepRecord]) -> int:
    return sum(1 for r in records if r.state == STATE_PLANNED)


def done_count(records: list[StepRecord]) -> int:
    return sum(1 for r in records if r.state == STATE_DONE)


def plan_rollback(records: list[StepRecord], to_index: int) -> dict[str, Any]:
    """What it takes to put the model back at ``to_index``.

    ``undo_count`` — how many FreeCAD transactions to undo. Only records that
    committed one count: a non-atomic execute_code/snapshot record carries no
    transaction, and counting it would undo a transaction that belongs to an
    EARLIER step (the undo stack is a plain stack).
    ``affected``   — the indices going back to ``planned``.
    ``blocking``   — non-atomic indices in that range that may have changed the
                     document without a transaction: undo would revert the wrong
                     change, so the caller must ask for force. A read-only
                     execute_code (mutated=False) is not one of them.
    ``accepted``   — reviewed indices in that range: rolling back across them
                     discards someone's approval, so the caller must force.
    """
    affected = [r for r in records if r.state == STATE_DONE and r.index > to_index]
    return {
        "undo_count": sum(1 for r in affected if r.transaction),
        "affected": [r.index for r in affected],
        "blocking": [r.index for r in affected if not r.atomic and r.mutated],
        "accepted": [r.index for r in affected if r.accepted],
    }


# --- review semantics --------------------------------------------------------


def plan_reject(records: list[StepRecord], index: int) -> dict[str, Any] | None:
    """What rejecting step ``index`` destroys: everything from it onward.

    Uniform semantics, mirroring "a new commit invalidates the planned tail":
    transaction-bearing done steps in the range are undone, planned ones
    dropped — the tail was authored against step ``index`` existing, so
    keeping it would replay steps against a world they were not designed
    for. None = no such step.
    """
    if not any(r.index == index for r in records):
        return None
    drop = [r for r in records if r.index >= index]
    done = [r for r in drop if r.state == STATE_DONE]
    return {
        "undo_count": sum(1 for r in done if r.transaction),
        "drop": [r.index for r in drop],
        "blocking": [r.index for r in done if not r.atomic and r.mutated],
        "accepted": [r.index for r in done if r.accepted],
    }


def set_accepted(records: list[StepRecord], index: int, on: bool = True) -> StepRecord | None:
    """Toggle the review marker on a done step. None = no such done step."""
    rec = next((r for r in records if r.index == index), None)
    if rec is None or rec.state != STATE_DONE:
        return None
    rec.accepted = bool(on)
    return rec


def update_planned(
    records: list[StepRecord],
    index: int,
    params: dict[str, Any] | None = None,
    label: str = "",
) -> StepRecord | None:
    """Edit a not-yet-committed step in place. None = done or unknown step.

    Top-level shallow merge (same rule as reexecute): ``obj_properties`` is
    replaced wholesale, so callers send the full property dict.
    """
    rec = next((r for r in records if r.index == index), None)
    if rec is None or rec.state not in (STATE_PLANNED, STATE_FAILED):
        return None
    if params:
        rec.params = {**(rec.params or {}), **params}
    if label:
        rec.label = label
    return rec


def insert_steps(
    records: list[StepRecord],
    after_index: int,
    steps: list[dict[str, Any]],
    executable_ops: set[str],
) -> list[StepRecord] | None:
    """Insert planned steps after ``after_index`` (a count, like run_to).

    The journal invariant "planned steps form the trailing run" means the
    insertion point must sit at or inside the tail; None = inside history
    (or empty steps).
    """
    if not steps:
        return None
    if after_index < planned_tail_start(records):
        return None
    added = [build_record(s, 0, STATE_PLANNED, executable_ops) for s in steps]
    records[after_index:after_index] = added
    reindex(records)
    return added


def invalidates_plan(records: list[StepRecord], objects_after: list[str]) -> bool:
    """Whether a mutation should discard the not-yet-executed planning tail.

    A cad() commit always does: the document moved under steps that were
    authored against the old state. An execute_code call is recorded here
    too, but it is very often a read — inspecting the model must not silently
    delete a plan — so it only invalidates the tail when the object set
    actually changed, compared against the last committed step's fingerprint.
    With nothing to compare against, keep the plan.
    """
    previous = next(
        (r.objects_after for r in reversed(records) if r.state == STATE_DONE and r.objects_after),
        [],
    )
    return bool(previous) and list(objects_after) != list(previous)


def last_atomic_done(records: list[StepRecord]) -> StepRecord | None:
    """The most recent completed step that owns an undo transaction.

    Drift detection anchors on this, not on the last completed step outright:
    a non-atomic record (execute_code) carries no transaction name, so
    anchoring on it would compare ``""`` against the undo stack and report a
    clean state even after the user undid work by hand.
    """
    return next(
        (r for r in reversed(records) if r.state == STATE_DONE and r.transaction),
        None,
    )


def rewind(records: list[StepRecord], count: int) -> list[StepRecord]:
    """Move the ``count`` most recently completed records back to ``planned``."""
    completed = [r for r in records if r.state == STATE_DONE]
    back = completed[-count:] if count > 0 else []
    for rec in back:
        rec.state = STATE_PLANNED
        rec.transaction = ""
        rec.error = ""
    return back
