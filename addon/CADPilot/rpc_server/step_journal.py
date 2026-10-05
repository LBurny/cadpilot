"""Step journal — the per-document record behind the steps panel.

Pure data model and arithmetic: NO FreeCAD imports, so it is unit-testable
from ``tests/`` without a running FreeCAD. The FreeCAD-facing half (document
property persistence, step execution, undo) lives in ``step_engine.py``.

Why a journal at all: the MCP-side modeling session (``session_state.py``)
logs steps for the LLM, but it lives in the MCP process and only exists while
a session is active. The panel must work in FreeCAD's own process, with the
RPC server stopped, so the addon keeps its own copy on the document.

Manual-edit sync lives half here, half in the engine: this module owns the
pure mapping decisions (which object/step owns a property, what a live value
maps back to), the engine's document observer owns the FreeCAD reads.
"""

from __future__ import annotations

import json
import math
import re
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
            rec = StepRecord.from_dict(raw)
        except (TypeError, ValueError):
            continue
        upgrade_legacy_label(rec)
        out.append(rec)
    return out


def reindex(records: list[StepRecord]) -> None:
    for i, rec in enumerate(records, start=1):
        rec.index = i


# --- one row-label grammar for every step ------------------------------------
#
# The panel's Step column answers one question: what IS this step? The caller's
# own ``description`` is the honest answer — it is the intent — and wins
# whenever it was written. With no description the label is derived from the
# op and the single parameter that identifies the step, so a row still reads
# as a step ("pad 'FlangeProfile' 6mm") rather than as a call ("pad on
# 'FlangeProfile'"). One grammar for every op, so a column of rows scans as a
# single document:
#
#     <verb> '<target>' <detail>
#
# The same builder feeds both halves of the journal — a committed step's label
# and a planned step's describe_step — so planned and done rows agree.

# The one scalar that names the step, with its unit. Key lookup is
# case-insensitive: cad() passes user params through verbatim, so a caller's
# spec may carry "Length" while the builders read "length".
_SCALAR_DETAIL: dict[str, tuple[str, str]] = {
    "pad": ("length", "mm"),
    "pocket": ("length", "mm"),
    "revolution": ("angle", "°"),
    "groove": ("angle", "°"),
    "fillet": ("radius", "mm"),
    "chamfer": ("size", "mm"),
    "thickness": ("value", "mm"),
    "draft": ("angle", "°"),
}

# Ops this grammar owns. Anything else (assemble, set_anchors, assembly, …)
# already writes a richer label of its own, and must not be "corrected" into
# the generic form.
_DERIVED_OPS = frozenset(
    {
        "create_object",
        "edit_object",
        "delete_object",
        "batch",
        "boolean",
        "fillet",
        "chamfer",
        "loft",
        "sweep",
        "mirror",
        "pattern",
        "move",
        "color",
        "variables",
        "sketch",
        "pad",
        "pocket",
        "revolution",
        "groove",
        "thickness",
        "draft",
        "datum_plane",
        "hull",
    }
)


def first_line(text: str | None) -> str:
    """A label is one line: the first non-empty line of a description block."""
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _num_text(value: Any) -> str:
    """A parameter as it should read in a label: 6.0 -> "6", "6 mm" as written."""
    if isinstance(value, str):
        return value.strip()
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def _params_ci(params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    return {str(k).lower(): v for k, v in params.items()}


def _color_text(value: Any) -> str:
    """A color parameter as it should read in a label: [0.8,.1,.1] -> "#cc1a1a".

    Local to this module on purpose: the label grammar is pure and must stay
    importable without FreeCAD, and property_mapper's parser imports it.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)) and len(value) in (3, 4):
        try:
            channels = [float(c) for c in value]
        except (TypeError, ValueError):
            return str(value)
        if max(channels[:3]) > 1.0:
            channels = [c / 255.0 for c in channels]
        return "#" + "".join(f"{max(0, min(255, round(c * 255))):02x}" for c in channels[:3])
    return str(value)


def feature_detail(operation: str, params: Any) -> str:
    """The ONE parameter that identifies a step ("6mm", "polar ×8"), else "".

    The Op column already names the operation; this names the thing, which is
    what turns a recorded call into a readable step when the caller wrote no
    description of its own.
    """
    p = _params_ci(params)
    op = str(operation or "")
    if p.get("through_all") and op in ("pocket", "pad"):
        # No length to show: this is PartDesign's through-all (parametric), and
        # the row must say so rather than fall back to a bare op name.
        return "through"
    scalar = _SCALAR_DETAIL.get(op)
    if scalar is not None:
        key, unit = scalar
        value = p.get(key)
        if value is None and key == "angle" and op in ("revolution", "groove"):
            # FreeCAD's default is a full revolve, and the row must not depend
            # on whether the caller spelled it out: "revolution 'Prof'" and
            # "revolution 'Prof' 360°" are the same step.
            value = 360
        if value is None:
            return ""
        text = _num_text(value)
        # A string already carries its own unit ("6 mm") or is an expression
        # ("=Vars.Thickness"); only a bare number needs the unit appended.
        return text if isinstance(value, str) else f"{text}{unit}"
    if op == "pattern":
        kind = "polar" if str(p.get("pattern_type", "linear")).lower() == "polar" else "linear"
        count = p.get("count")
        return f"{kind} ×{_num_text(count)}" if count is not None else kind
    if op == "boolean":
        return str(p.get("op") or "")
    if op == "mirror":
        return f"across {str(p.get('plane') or 'XY').upper()}"
    if op == "sweep":
        return f"along '{p['path']}'" if p.get("path") else ""
    if op == "move":
        delta = p.get("translate")
        if isinstance(delta, dict):
            delta = [delta.get("x", 0), delta.get("y", 0), delta.get("z", 0)]
        if isinstance(delta, (list, tuple)) and len(delta) == 3:
            return "Δ(" + ", ".join(_num_text(v) for v in delta) + ")"
        rotate = p.get("rotate")
        if isinstance(rotate, dict) and rotate.get("angle") is not None:
            return f"rotate {_num_text(rotate['angle'])}°"
        return "placement" if p.get("placement") else ""
    if op == "variables":
        cells = p.get("cells")
        return f"{len(cells)} cell(s)" if isinstance(cells, dict) and cells else ""
    if op == "color":
        bits = []
        if p.get("color") is not None:
            bits.append(_color_text(p["color"]))
        if p.get("transparency") is not None:
            bits.append(f"{_num_text(p['transparency'])}%")
        if not bits:
            for key in ("line_color", "display_mode", "draw_style"):
                if p.get(key) is not None:
                    bits.append(_color_text(p[key]) if key == "line_color" else str(p[key]))
                    break
        if not bits and p.get("visible") is not None:
            bits.append("show" if p["visible"] else "hide")
        return " ".join(bits)
    if op == "sketch":
        bits = []
        for key, word in (("geometry", "geom"), ("constraints", "con")):
            count = len(p.get(key) or [])
            if count:
                bits.append(f"{count} {word}")
        return " / ".join(bits)
    if op == "loft":
        count = len(p.get("profiles") or [])
        return f"{count} profiles" if count else ""
    if op == "hull":
        views = p.get("sketches")
        count = len(views) if isinstance(views, (list, dict)) else 0
        return f"{count} views" if count else ""
    if op == "datum_plane":
        plane = p.get("plane")
        if isinstance(plane, dict):
            plane = plane.get("datum") or plane.get("plane")
        return str(plane) if plane else ""
    return ""


def batch_label(ops: Any) -> str:
    """ "batch ×5: pad, pocket, fillet" — what a batch row should say.

    "batch (5 ops)" described the container, not the step; the distinct
    sub-op verbs make the row scannable without opening it.
    """
    verbs: list[str] = []
    for sub in ops or []:
        if not isinstance(sub, dict):
            continue
        verb = str(sub.get("action") or sub.get("operation") or "")
        if verb and verb not in verbs:
            verbs.append(verb)
    head = f"batch ×{len(ops or [])}"
    if not verbs:
        return head
    tail = ", ".join(verbs[:4]) + (", …" if len(verbs) > 4 else "")
    return f"{head}: {tail}"


def step_label(
    operation: str,
    target: str | None = "",
    params: Any = None,
    description: str | None = "",
    detail: str | None = None,
) -> str:
    """The canonical one-line label for a step.

    The caller's description when it wrote one, else the derived
    ``<verb> '<target>' <detail>`` form. ``detail`` overrides the derived
    detail for the few ops whose identifying text is not a spec parameter
    (create_object's type).
    """
    note = first_line(description)
    if note:
        return note
    op = str(operation or "")
    if detail is None:
        detail = feature_detail(op, params)
    name = str(target or "")
    head = f"{op} '{name}'" if name else op
    return f"{head} {detail}".strip() if detail else head


def derived_label(rec: StepRecord) -> str:
    """A recorded step's mechanical label, recomputed from its stored params.

    Used by the hover tooltip: when the row label is the caller's description,
    this is the only place the operation and its parameters still surface.
    "" for the ops that write their own label (assemble, set_anchors, …).
    """
    op = rec.operation
    if op not in _DERIVED_OPS:
        return ""
    params = rec.params or {}
    if op == "batch":
        ops = params.get("ops")
        return batch_label(ops) if ops else ""
    if op == "create_object":
        name = params.get("obj_name")
        if not name:
            return ""
        obj_type = params.get("obj_type")
        return step_label("create", name, detail=f"({obj_type})" if obj_type else "")
    if op in ("edit_object", "delete_object"):
        name = params.get("obj_name")
        return step_label(op.split("_", 1)[0], name) if name else ""
    name = params.get("obj_name")
    if not name:
        return ""
    return step_label(op, name, params.get("obj_properties"))


def upgrade_legacy_label(rec: StepRecord) -> bool:
    """Re-label a step recorded before the label grammar existed.

    The old auto labels were "<op> on '<target>'", "batch (N ops)" and
    "create <type> '<name>'". They carry nothing the derived label does not,
    and a caller's description never looks like them, so upgrading is
    lossless: a model built before the change reads as a set of steps rather
    than a set of calls the moment it is opened. Returns whether it changed.
    """
    params = rec.params or {}
    name = params.get("obj_name")
    legacy = {
        f"{rec.operation} on '{name}'",
        f"batch ({len(params.get('ops') or [])} ops)",
        f"create {params.get('obj_type')} '{name}'",
    }
    if rec.label not in legacy:
        return False
    upgraded = derived_label(rec)
    if not upgraded or upgraded == rec.label:
        return False
    rec.label = upgraded
    return True


def describe_step(step: dict[str, Any]) -> str:
    """Derived label for a *planned* step (a cad() argument dict).

    Shares step_label with the committed path, so a planned row and the same
    step after it ran read identically instead of drifting apart.
    """
    op = str(step.get("operation") or step.get("action") or "")
    if op == "execute_code":
        # A planned snippet has no effect yet, so its row is its own leading
        # comment; without one there is nothing honest to show.
        return snippet_description(str(step.get("code") or "")) or "execute_code"
    if op not in _DERIVED_OPS:
        # Ops outside the grammar (align_shapes, assemble, …) keep the plain
        # "<op> '<target>'" form rather than losing their target entirely.
        name = step.get("obj_name")
        return f"{op} '{name}'" if name else op
    note = step.get("description")
    if op == "batch":
        return batch_label(step.get("ops"))
    if op == "create_object":
        obj_type = step.get("obj_type")
        return step_label(
            "create",
            step.get("obj_name"),
            description=note,
            detail=f"({obj_type})" if obj_type else "",
        )
    if op in ("edit_object", "delete_object"):
        return step_label(op.split("_", 1)[0], step.get("obj_name"), description=note)
    return step_label(op, step.get("obj_name"), step.get("obj_properties"), description=note)


def params_for(step: dict[str, Any]) -> dict[str, Any]:
    """The exact payload ``step_engine.execute_record`` needs to re-run a step.

    Everything except routing/presentation keys passes through: create/edit use
    ``obj_name``/``obj_type``/``obj_properties``, batches use ``ops``, and the
    assembly ops carry their own payloads (``mates``, ``anchors``, ``spec``, …).
    Whitelisting keys here used to silently strip an assemble step's mates, so
    a planned assemble could never run.
    """
    return {
        k: v for k, v in step.items() if k not in ("operation", "action", "description", "label")
    }


def sub_operation(sub: dict[str, Any]) -> str:
    """A batch sub-op's operation name, tolerating both key conventions.

    cad() batch ops arrive as {"action": ...} (the RPC batch schema) and are
    journaled verbatim, while journal-native steps use {"operation": ...} —
    without this, re-running a recorded batch step resolves "" and dies with
    "operation '' is not re-executable".
    """
    return str(sub.get("operation") or sub.get("action") or "")


def objects_after_index(records: list[StepRecord], index: int) -> list[str]:
    """The object list recorded for the last done step at or before ``index``."""
    names: list[str] = []
    for rec in records:
        if rec.index <= index and rec.state == STATE_DONE:
            names = list(rec.objects_after or [])
    return names


def created_since(records: list[StepRecord], index: int) -> list[str]:
    """Objects done steps after ``index`` introduced, from before/after diffs.

    Diffs, never whole snapshots: ``objects_after`` lists the ENTIRE document,
    so subtracting one step's snapshot from another's drags in objects that
    predate the journal. At ``index`` 0 the target snapshot is empty by
    definition, and a rollback must never delete what the journal did not
    build — the user's own objects sit in every snapshot. A record without a
    before-snapshot (journals written before ``objects_before`` existed)
    falls back to the previous done record's after-list, which is the same
    document one step earlier.
    """
    created: set[str] = set()
    prev_after: set[str] = set()
    for rec in records:
        if rec.state != STATE_DONE:
            continue
        before = set(rec.objects_before) if rec.objects_before else prev_after
        if rec.index > index:
            created |= set(rec.objects_after or []) - before
        if rec.objects_after:
            prev_after = set(rec.objects_after)
    return sorted(created)


def steps_without_undo(records: list[StepRecord], index: int) -> list[int]:
    """Done steps after ``index`` that own no transaction.

    These are the ones a rollback cannot actually undo: FreeCAD's undo stack
    holds nothing for them, so whatever they changed survives the rollback. A
    journal that mixed transactional steps with older non-transactional ones is
    exactly how a rollback ends up reporting success while the model keeps the
    objects it was asked to drop.
    """
    return [
        rec.index
        for rec in records
        if rec.index > index and rec.state == STATE_DONE and rec.mutated and not rec.transaction
    ]


def unrecoverable_steps(records: list[StepRecord], index: int) -> list[int]:
    """Done steps up to ``index`` that nothing can put back.

    Such a step may have changed the document, yet it owns no transaction (so
    FreeCAD's undo cannot reach it) and is not re-executable (its operation or
    code was never recorded, as in journals written before execute_code became
    transactional). A rollback that hits one cannot restore the model exactly.
    """
    return [
        rec.index
        for rec in records
        if rec.index <= index and rec.state == STATE_DONE and rec.mutated and not rec.executable
    ]


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


# A PEP-263 coding cookie ("# -*- coding: utf-8 -*-", "# coding=latin-1") is
# encoding boilerplate, never a step description.
_CODING_RE = re.compile(r"^#.*\bcoding[:=]")


def snippet_description(code: str) -> str:
    """The execute_code description convention: the snippet's LEADING comment
    block is the step's human description ("# 步骤1: 琴身轮廓 + f孔").

    Leading blank lines, a shebang and the coding cookie are boilerplate and
    skipped; the block ends at its first blank or non-comment line — a later
    comment is an implementation note, not the description. "" = the snippet
    carries no description (callers fall back to the effect label).
    """
    lines: list[str] = []
    for raw in str(code or "").splitlines():
        line = raw.strip()
        if not lines and (not line or line.startswith("#!") or _CODING_RE.match(line)):
            continue
        if not line.startswith("#"):
            break
        lines.append(line[1:].strip())
    return "\n".join(lines).strip()


def execute_code_label(code: str, changed: bool, before: list[str], after: list[str]) -> str:
    """The row label recorded for an execute_code step.

    One label, wherever it is read: the snippet's LEADING comment block says
    what the code IS (the documented convention), the effect says what it DID.
    The label used to be the effect alone, so the panel showed the description
    (composed in row_text) while step_control(status) and the MCP journal
    snapshot showed "execute_code: read-only" for the same step — two names for
    one row. Legacy labels of that old form are still rendered correctly by
    row_text, so reading a journal written by an older addon stays lossless.
    """
    effect = effect_label(changed, before, after)
    desc = snippet_description(code)
    if desc:
        return f"{desc.splitlines()[0]} · {effect}"
    return f"execute_code: {effect}"


# A recorded snippet captures the document NAME it ran against
# ("App.getDocument('MideaDeskFan')"). Save the file under a new name and
# reopening hands FreeCAD a document named after the FILE — every reference
# then dies with "Unknown document" and a rebuild cannot even start.
_DOC_REF_RE = re.compile(r"\bgetDocument\(\s*(['\"])([^'\"]+)\1\s*\)")


def document_refs(code: str) -> set[str]:
    """Document names a snippet resolves through ``getDocument(<literal>)``."""
    return {m.group(2) for m in _DOC_REF_RE.finditer(code or "")}


def rewrite_document_refs(code: str, mapping: dict[str, str]) -> str:
    """Re-point ``getDocument`` literals per ``mapping`` (old name -> new)."""

    def sub(m: re.Match) -> str:
        name = mapping.get(m.group(2), m.group(2))
        return f"getDocument({m.group(1)}{name}{m.group(1)})"

    return _DOC_REF_RE.sub(sub, code or "")


def step_description(rec: StepRecord) -> str:
    """A step's human description — one rule for every op, so the panel has a
    single canonical text to show (row, tooltip, spotlight).

    execute_code: the snippet's leading comment block (snippet_description) —
    the recorded label only says what the snippet DID, not what it IS. Every
    other op: the recorded label, which is already human text ("pad 'Pad'", a
    snapshot's note).
    """
    if rec.operation == "execute_code":
        return snippet_description(str((rec.params or {}).get("code") or ""))
    return rec.label


def row_text(rec: StepRecord) -> str:
    """The panel tree row for one step.

    An execute_code row leads with its description (what the snippet IS) and
    keeps the effect (what it DID) behind it. A record written by this addon
    already carries that composed label; a legacy record carries only the
    effect, so the description is composed here. The description is read off
    the CURRENT params, so editing the snippet's leading comment updates the row
    on the next refresh. Pure so the display rule is unit-testable without Qt.
    """
    label = rec.label
    desc = step_description(rec)
    if rec.operation == "execute_code" and desc:
        first = desc.splitlines()[0]
        if not label.startswith(first):
            label = f"{first} · {label.removeprefix('execute_code: ')}"
    if rec.error:
        label += f"  — {rec.error}"
    return label


def tooltip_text(rec: StepRecord) -> str:
    """Full row text for hover: the row label, then what the step actually did.

    When the label IS the caller's description, the derived text is the only
    place the operation and its parameters survive — so the tooltip keeps
    both. For a step with no description the two are equal and the label
    alone is the tooltip.
    """
    if rec.operation == "execute_code":
        desc = step_description(rec)
        if desc:
            first = desc.splitlines()[0]
            # The label is "description · effect" (this addon) or "execute_code:
            # effect" (legacy); either way the effect is what follows the
            # description, so the tooltip can show the full comment block.
            if rec.label.startswith(first):
                effect = rec.label[len(first) :].lstrip(" ·")
            else:
                effect = rec.label.removeprefix("execute_code: ")
            return f"{desc}\n{effect}" if effect else desc
        return rec.label
    derived = derived_label(rec)
    if derived and derived != rec.label:
        return f"{rec.label}\n{derived}"
    return rec.label


def build_record(
    step: dict[str, Any],
    index: int,
    state: str,
    executable_ops: set[str],
    label: str = "",
) -> StepRecord:
    op = str(step.get("operation", ""))
    # A planned execute_code step is executable when it CARRIES its snippet:
    # _execute_one re-runs recorded code through the same executor the RPC
    # handler uses, so the op name simply was not in EXECUTABLE_OPS — a plan
    # holding a snippet used to be accepted and then silently skipped at run
    # time ("skipped: not re-executable").
    executable = op in executable_ops or (op == "execute_code" and bool(step.get("code")))
    return StepRecord(
        index=index,
        state=state,
        operation=op,
        label=label or describe_step(step),
        params=params_for(step),
        executable=executable,
        timestamp=stamp(),
    )


def planned_tail_start(records: list[StepRecord]) -> int:
    """Index of the first record in the trailing run of ``planned`` records."""
    n = len(records)
    while n > 0 and records[n - 1].state == STATE_PLANNED:
        n -= 1
    return n


def drop_planned(records: list[StepRecord]) -> int:
    """Discard every ``planned`` record, wherever it sits; returns the count.

    A new commit invalidates the not-yet-executed plan, and the plan is NOT
    guaranteed to be a trailing run: read-only inspections are appended AFTER
    it (so index-addressed verbs see stable numbers), and a snapshot marker
    may already sit behind it. Slicing from ``planned_tail_start`` then removes
    nothing and a stale plan survives the commit — removal has to key on
    STATE, not position.
    """
    keep = [r for r in records if r.state != STATE_PLANNED]
    dropped = len(records) - len(keep)
    records[:] = keep
    return dropped


def set_plan(
    records: list[StepRecord], steps: list[dict[str, Any]], executable_ops: set[str]
) -> list[StepRecord]:
    """Replace the not-yet-executed plan with a fresh plan."""
    drop_planned(records)
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


def set_label(records: list[StepRecord], index: int, label: str) -> StepRecord | None:
    """Rename any recorded step (planned, failed or DONE).

    ``update`` edits planned/failed params, and a done step's params must not
    be edited out of the review loop (its transaction is history) — but its
    LABEL is presentation, and "the row says something wrong" is a reason to
    fix the row, not to re-run the step. None = unknown step or empty label.
    """
    if not label:
        return None
    rec = next((r for r in records if r.index == index), None)
    if rec is None:
        return None
    rec.label = label
    return rec


def insert_steps(
    records: list[StepRecord],
    after_index: int,
    steps: list[dict[str, Any]],
    executable_ops: set[str],
) -> list[StepRecord] | None:
    """Insert planned steps after ``after_index`` (a count, like run_to).

    Planned steps form ONE contiguous run, but done records may sit behind it
    (appended inspections, a snapshot marker), so the insertion point must
    land at or inside the run — inserting after those done records would
    strand planned steps behind done ones. None = outside the run (or empty
    steps).
    """
    if not steps:
        return None
    planned = [r.index for r in records if r.state == STATE_PLANNED]
    if planned:
        # after_index == planned[0] - 1 lands at the run's head (the old
        # tail-start rule); anything before that is executed history.
        if not (planned[0] - 1 <= after_index <= planned[-1]):
            return None
    elif after_index < len(records):
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


# --- manual-edit sync (pure half) ---------------------------------------------
#
# The engine's document observer mirrors GUI edits on objects a done step
# produced back into that step's params, so a later reexecute/replay does not
# silently revert a human correction. Everything decidable without FreeCAD
# lives here: which object/step owns which property (tracked_objects), and
# what a live value maps back to (map_cell_value / map_constraint_value).

# Feature op -> {object property: spec key}. Only properties the builder
# actually consumes are mapped, so a synced value is always a valid spec key —
# including ones the caller did not pass originally (a pad built with the
# default length must still pick up a GUI Length edit, or replay reverts it).
# Richer values (edge selectors, links, boolean tool compounds) stay unsynced.
# FreeCAD >= 1.1 moved Part-level fillet/chamfer sizes into per-edge Edges
# tuples, so both names are claimed (only the one that exists fires).
FEATURE_SYNC: dict[str, dict[str, str]] = {
    "fillet": {"Radius": "radius", "Edges": "radius"},
    "chamfer": {"Size": "size", "Edges": "size"},
    "pad": {"Length": "length", "Reversed": "reversed", "Midplane": "midplane"},
    "pocket": {"Length": "length", "Reversed": "reversed", "Midplane": "midplane"},
    "revolution": {"Angle": "angle", "Reversed": "reversed"},
    "groove": {"Angle": "angle", "Reversed": "reversed"},
    "thickness": {"Value": "value", "Reversed": "reversed"},
    "draft": {"Angle": "angle"},
    "loft": {"Solid": "solid", "Ruled": "ruled"},
    "sweep": {"Solid": "solid"},
    # A datum plane / attached sketch offset is a pure-z AttachmentOffset;
    # the engine reader refuses anything more general (rotation, x/y shift).
    "datum_plane": {"AttachmentOffset": "offset"},
    "sketch": {"AttachmentOffset": "offset"},
}

# Ops whose obj_name names the object they CREATE (not a base), so the object
# is tracked by name even when the before/after diff is empty (an idempotent
# variables re-run creates nothing, and without this its sheet never syncs).
NEW_OBJECT_OPS = {"variables", "sketch", "datum_plane", "hull"}

# Spec constraint type -> live Sketcher.Constraint.Type. Used to verify that
# the live constraint sequence still lines up with the spec by index before
# any value is synced (a hand-added constraint shifts indices — then nothing
# is synced).
CONSTRAINT_TYPE_MAP = {
    "coincident": "Coincident",
    "horizontal": "Horizontal",
    "vertical": "Vertical",
    "tangent": "Tangent",
    "perpendicular": "Perpendicular",
    "parallel": "Parallel",
    "equal": "Equal",
    "symmetric": "Symmetric",
    "distance": "Distance",
    "distance_x": "DistanceX",
    "distance_y": "DistanceY",
    "radius": "Radius",
    "angle": "Angle",
}

DIMENSIONAL_CONSTRAINTS = ("distance", "distance_x", "distance_y", "radius", "angle")

#: map_cell_value / map_constraint_value return this when the live state has
#: no safe spec representation (the params are left untouched).
UNREADABLE = object()


def _num(value: float) -> int | float:
    f = float(value)
    return int(f) if f.is_integer() else f


def _batch_sync_ops(rec: StepRecord) -> list[tuple[int, str, dict[str, Any]]]:
    """(position, op name, sub-op dict) of a batch record's syncable sub-ops.

    create/edit/move sub-ops carry the payload a manual edit maps back onto;
    feature sub-ops are skipped (see tracked_objects).
    """
    out: list[tuple[int, str, dict[str, Any]]] = []
    for i, sub in enumerate((rec.params or {}).get("ops") or []):
        if not isinstance(sub, dict):
            continue
        op = sub_operation(sub)
        if op in ("create_object", "edit_object", "move"):
            out.append((i, op, sub))
    return out


def tracked_objects(records: list[StepRecord]) -> dict[str, dict[str, Any]]:
    """object name -> sync handlers, from done steps.

    Entry keys:
      ``props``  — {object property: (step index, params key)}. Later done
                   steps win per property, so a sync lands on the step that
                   last decided it. create/edit_object also claim Placement —
                   unless a move step targets the object, because a move is
                   relative and an absolute create-time Placement plus the
                   move would double-apply on reexecute.
      ``sheet``  — index of the variables step owning the spreadsheet.
      ``sketch`` — index of the sketch step owning the sketch.
      ``move``   — (index, sub) of the LAST done move targeting the object
                   (top-level step or a move sub-op inside a batch). A manual
                   drag folds into it as an absolute placement override
                   (placement wins over translate/rotate on re-run), which
                   reproduces the pose no matter what came before it.

    Property claims are ``(step index, params key, batch sub-op position)``
    with sub None for top-level steps; the engine routes the sync write
    through it. Batch coverage: create/edit/move SUB-OPS sync (their payload
    names the object and carries the scalars a manual edit maps back onto);
    feature sub-ops do not — their created object cannot be attributed from
    the record-level object diff.
    """
    last_move: dict[str, tuple[int, int | None]] = {}
    for r in records:
        if r.state != STATE_DONE:
            continue
        if r.operation == "move":
            name = str((r.params or {}).get("obj_name") or "")
            if name:
                last_move[name] = (r.index, None)
        elif r.operation == "batch":
            for sub_i, sub_op, sub in _batch_sync_ops(r):
                if sub_op == "move":
                    name = str(sub.get("obj_name") or "")
                    if name:
                        last_move[name] = (r.index, sub_i)

    tracked: dict[str, dict[str, Any]] = {}
    for rec in records:
        if rec.state != STATE_DONE:
            continue
        params = rec.params or {}
        if rec.operation == "batch":
            for sub_i, sub_op, sub in _batch_sync_ops(rec):
                if sub_op == "move":
                    continue  # the fold handler owns move sub-ops
                props = sub.get("obj_properties") or {}
                claims = {k: k for k, v in props.items() if isinstance(v, (int, float, str, bool))}
                name = str(sub.get("obj_name") or "")
                if not name:
                    continue
                entry = tracked.setdefault(
                    name, {"props": {}, "sheet": None, "sketch": None, "move": None}
                )
                for prop, key in claims.items():
                    entry["props"][prop] = (rec.index, key, sub_i)
                if sub_op == "create_object" and name not in last_move:
                    entry["props"]["Placement"] = (rec.index, "Placement", sub_i)
            continue
        props = params.get("obj_properties") or {}
        op = rec.operation
        if op in ("create_object", "edit_object"):
            claims = {k: k for k, v in props.items() if isinstance(v, (int, float, str, bool))}
            if op == "create_object":
                names = set(rec.objects_after) - set(rec.objects_before)
            else:
                names = {str(params.get("obj_name") or "")}
        else:
            claims = dict(FEATURE_SYNC.get(op, {}))
            names = set(rec.objects_after) - set(rec.objects_before)
            if op in NEW_OBJECT_OPS:
                names |= {str(params.get("obj_name") or "")}
        for name in names:
            if not name:
                continue
            entry = tracked.setdefault(
                name, {"props": {}, "sheet": None, "sketch": None, "move": None}
            )
            for prop, key in claims.items():
                entry["props"][prop] = (rec.index, key, None)
            if op in ("create_object", "edit_object") and name not in last_move:
                entry["props"]["Placement"] = (rec.index, "Placement", None)
            # The sheet/sketch handlers belong to the object the step NAMES —
            # the diff can also hold incidental creations (a sketch's Body),
            # which must not answer constraint/cell lookups.
            if name == str(params.get("obj_name") or ""):
                if op == "variables":
                    entry["sheet"] = rec.index
                elif op == "sketch":
                    entry["sketch"] = rec.index

    for name, (idx, sub) in last_move.items():
        entry = tracked.setdefault(name, {"props": {}, "sheet": None, "sketch": None, "move": None})
        entry["move"] = (idx, sub)
        entry["props"].pop("Placement", None)
    return tracked


def map_cell_value(old: Any, live_contents: Any) -> Any:
    """A variables-cell's synced value from the raw cell content.

    ``Sheet.get(cell)`` returns the COMPUTED value, which is stale when the
    change event fires (the recompute has not run yet) — live-verified: at
    event time get() still held the previous value while getContents() was
    already fresh. So only ``Sheet.getContents(cell)`` is read, whose 1.1.4
    conventions were measured live (ordinals, not reprs):

    * ``=A1 * 2``   — formula: stored whitespace-normalised (FreeCAD
                      pretty-prints on read, and without normalising every
                      read of a compactly written spec looks like an edit);
    * ``'...``      — text: a LEADING apostrophe marks a text cell
                      (Excel-style; a trailing one is optional). The builder
                      quotes text values, and the quotes persist in the cell
                      text, so one surrounding double-quote pair is stripped
                      too (the spec stores the bare text);
    * ``25``        — number.
    Returns UNREADABLE when the content cannot be read back safely.
    """
    if not isinstance(live_contents, str):
        return UNREADABLE
    c = live_contents.strip()
    if not c:
        return UNREADABLE  # a cleared cell has no spec representation
    if c.startswith("="):
        return "=" + "".join(c[1:].split())
    if c.startswith("'"):
        text = c[1:].removesuffix("'")
        if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        return text
    try:
        return _num(float(c))
    except ValueError:
        return UNREADABLE


def map_constraint_value(ctype: str, old: Any, live_value: Any, live_expr: str | None) -> Any:
    """A dimensional sketch constraint's synced value.

    ``live_value`` reads back in RADIANS for angle constraints (the spec takes
    degrees — same asymmetry as FreeCAD.Rotation). A live expression binding
    wins over the literal in both directions: binding in the GUI turns the
    spec value into ``=expr``, unbinding turns it back into the datum number.
    """
    if live_expr:
        return "=" + "".join(str(live_expr).split())
    try:
        value = float(live_value)
    except (TypeError, ValueError):
        return UNREADABLE
    if ctype == "angle":
        value = math.degrees(value)
    return _num(value)
