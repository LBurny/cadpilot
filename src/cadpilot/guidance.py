"""Lightweight guidance for modeling sessions (CAD-trimmed from nsforge).

Only heuristics that are genuinely useful while modeling: what to do next
based on the document state, and risks that make rollback unreliable.
Deliberately omitted from the nsforge original: goal/progress tracking,
derivation patterns, and step replay verification.
"""

from __future__ import annotations

from .session_state import ModelingSession

# Ops that count as "sketch mode" activity for the primitive_without_sketch
# heuristic (designing parts from constrained sketches, not raw primitives).
_SKETCH_OPS = {
    "sketch",
    "pad",
    "pocket",
    "revolution",
    "groove",
    "hull",
    "datum_plane",
    "variables",
}


def suggest_next_steps(
    session: ModelingSession,
    object_names: list[str],
) -> list[dict[str, str]]:
    suggestions: list[dict[str, str]] = []

    if not object_names:
        suggestions.append(
            {
                "tool": "cad",
                "operation": "create_object",
                "reason": "The document is empty — create the first solid "
                "(e.g. Part::Box / Part::Cylinder)",
            }
        )
        return suggestions

    non_atomic = [s.step_number for s in session.steps if not s.atomic]
    if non_atomic:
        suggestions.append(
            {
                "tool": "cad",
                "operation": "batch",
                "reason": f"Step(s) {non_atomic} used execute_code and cannot be rolled back "
                "reliably; prefer cad() mutations for undoable steps",
            }
        )

    # Multiple parts and no assembly activity yet → assembly mode.
    if len(object_names) >= 2:
        from . import assembly_state as _astate  # lazy: read-only registry probe

        has_assembly = (
            any(s.operation == "assemble" for s in session.steps)
            or _astate.current_session() is not None
        )
        if not has_assembly:
            suggestions.append(
                {
                    "tool": "assembly_session",
                    "operation": "start",
                    "reason": "Multiple parts in the document — assembly mode mates them "
                    "with persistent joints (start → add_component → mate → solve)",
                }
            )

    if session.step_count >= 10 and session.status == "active":
        suggestions.append(
            {
                "tool": "session_complete",
                "operation": "",
                "reason": f"{session.step_count} steps recorded — consider completing the "
                "session to save the workflow into the pattern store",
            }
        )

    suggestions.append(
        {
            "tool": "get_view",
            "operation": "",
            "reason": "Visually check the current model state if unsure",
        }
    )
    return suggestions[:3]


def detect_risks(
    session: ModelingSession,
    doc_open: bool,
    object_names: list[str] | None,
) -> list[dict[str, str]]:
    risks: list[dict[str, str]] = []

    if not doc_open:
        risks.append(
            {
                "level": "warning",
                "type": "document_closed",
                "message": f"Session document '{session.doc_name}' is not open in FreeCAD "
                "(closed externally?). Rollback and mutations will fail.",
            }
        )
        return risks

    non_atomic = [s.step_number for s in session.steps if not s.atomic]
    if non_atomic:
        risks.append(
            {
                "level": "warning",
                "type": "non_atomic_steps",
                "message": f"Step(s) {non_atomic} were recorded via execute_code without a "
                "transaction; session_rollback past them may undo the wrong change.",
            }
        )

    # Fingerprint drift: the document's objects no longer match what the last
    # recorded step saw — someone/something edited the document outside cad().
    if object_names is not None and session.steps:
        expected = session.steps[-1].objects_after
        if expected and sorted(object_names) != expected:
            risks.append(
                {
                    "level": "warning",
                    "type": "state_drift",
                    "message": "Document objects differ from the last recorded step — "
                    "the model was edited outside cad() (GUI?). Rollback may "
                    "undo those external edits too.",
                }
            )

    # Connectivity: the last committed mutation's auto-audit found parts not
    # touching the main assembly (recorded in the step's result_summary).
    if session.steps and "Connectivity:" in session.steps[-1].result_summary:
        detail = (
            session.steps[-1].result_summary.split("Connectivity:", 1)[1].split("\n", 1)[0].strip()
        )
        risks.append(
            {
                "level": "warning",
                "type": "disconnected_islands",
                "message": f"Last mutation left disconnected islands: {detail} — "
                "fix gaps so parts touch (≤0.5mm) or intersect.",
            }
        )

    # Sketch-mode steer: several raw Part:: primitives and no sketch activity.
    primitive = [
        s
        for s in session.steps
        if s.operation == "create_object" and s.params_summary.startswith("Part::")
    ]
    if len(primitive) >= 2 and not any(s.operation in _SKETCH_OPS for s in session.steps):
        risks.append(
            {
                "level": "info",
                "type": "primitive_without_sketch",
                "message": f"{len(primitive)} raw Part:: primitives created and no sketch "
                "used — sketch mode (constrained sketch → pad/revolution/hull) "
                "gives parametric, editable parts.",
            }
        )

    # Blind placement: steps that set an absolute Placement (marker added by
    # cad_operation in the step's params_summary).
    placed = [s.step_number for s in session.steps if "+Placement" in s.params_summary]
    if placed:
        risks.append(
            {
                "level": "info",
                "type": "absolute_placement",
                "message": f"Step(s) {placed} set an absolute Placement — blind coordinates "
                "cause floating parts. Prefer move/align_shapes/anchors "
                "(free mode) or assembly_session mates.",
            }
        )

    if session.redo_buffer:
        risks.append(
            {
                "level": "info",
                "type": "redo_available",
                "message": f"{len(session.redo_buffer)} undone step(s) can be restored with "
                "session_redo — any new cad() step discards them.",
            }
        )
    return risks
