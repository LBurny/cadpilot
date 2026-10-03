"""step_engine dispatch consistency — the MCP verb set must match the addon's.

The engine runs inside FreeCAD and cannot be imported from the test suite, so
this parses its source: the verbs step_control accepts are a contract between
``operations/core.py`` and ``step_engine._apply_op``, and this test is what
keeps the two sides from drifting apart.
"""

import ast
from pathlib import Path

_ADDON = Path(__file__).resolve().parents[1] / "addon" / "CADPilot"
_ENGINE = ast.parse((_ADDON / "rpc_server" / "step_engine.py").read_text(encoding="utf-8"))

JOURNAL_OPS = {
    "status",
    "set_plan",
    "clear_plan",
    "run_next",
    "run_all",
    "run_to",
    "rollback_to",
    "reexecute",
    "accept",
    "reject",
    "update",
    "insert",
    "replay",
    "reset",
}


def _comparison_strings(tree) -> set[str]:
    return {
        n.value
        for cmp in ast.walk(tree)
        if isinstance(cmp, ast.Compare)
        for n in [cmp.left, *cmp.comparators]
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def test_every_journal_op_is_dispatched():
    strings = _comparison_strings(_ENGINE)
    missing = JOURNAL_OPS - strings
    assert not missing, f"step_engine._apply_op does not handle: {sorted(missing)}"


def test_batch_is_executable():
    """A plan containing a cad() batch step must be runnable, not just storable."""
    for node in ast.walk(_ENGINE):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "EXECUTABLE_OPS" for t in node.targets
        ):
            strings = {
                n.value
                for n in ast.walk(node.value)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            }
            assert "batch" in strings
            return
    raise AssertionError("EXECUTABLE_OPS assignment not found")


def test_run_steps_skips_non_executable_records():
    """run_all/replay must not die on a non-executable record — an execute_code
    inspection is a normal journal citizen, and hard-failing on it made replay
    unusable after any inspection. _run_steps needs an executable guard that
    marks the record done and continues, and must report the skips."""
    func = next(
        n for n in ast.walk(_ENGINE) if isinstance(n, ast.FunctionDef) and n.name == "_run_steps"
    )
    guards = [
        n
        for n in ast.walk(func)
        if isinstance(n, ast.If)
        and any(isinstance(m, ast.Attribute) and m.attr == "executable" for m in ast.walk(n.test))
        and any(isinstance(m, ast.Continue) for s in n.body for m in ast.walk(s))
    ]
    assert guards, "_run_steps must skip (continue) records that are not executable"
    strings = {
        n.value for n in ast.walk(func) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert "skipped" in strings


def test_rollback_verifies_the_undo_before_promising_native():
    """The undo stack is shared with the GUI: a manual edit interleaved on it
    pops under a rollback's name while the popped count still matches, so the
    count alone certified nothing and a rollback reported "native" over a
    wrong model. _rollback must compare the object sets (sj.created_since
    leftovers against sj.objects_after_index expectations) after undo_n."""
    func = next(
        n for n in ast.walk(_ENGINE) if isinstance(n, ast.FunctionDef) and n.name == "_rollback"
    )
    attrs = {n.attr for n in ast.walk(func) if isinstance(n, ast.Attribute)}
    assert "created_since" in attrs, "_rollback must compute leftovers via sj.created_since"
    assert (
        "objects_after_index" in attrs
    ), "_rollback must verify the object set expected at the target step"


def test_removal_sets_come_from_record_diffs():
    """Removal sets must be sj.created_since (before/after diffs) everywhere:
    whole-snapshot subtraction at index 0 has an empty target, and since every
    snapshot lists the entire document it would delete objects that predate
    the journal — the user's own work."""
    attrs = {n.attr for n in ast.walk(_ENGINE) if isinstance(n, ast.Attribute)}
    assert "extra_objects" not in attrs, "the whole-snapshot variant is the data-loss bug"
    for fname in ("_rollback", "_reject", "_reexecute"):
        func = next(
            n for n in ast.walk(_ENGINE) if isinstance(n, ast.FunctionDef) and n.name == fname
        )
        assert "created_since" in {
            n.attr for n in ast.walk(func) if isinstance(n, ast.Attribute)
        }, f"{fname} must compute its removal/verification set via sj.created_since"


def test_rebuild_resets_failed_records_for_the_re_run():
    """A failed step's transaction aborted (nothing to clean up), and a rebuild
    that leaves it failed would run a LATER step against its missing result.
    The reset loop in _rollback must cover failed records, not only done
    ones."""
    func = next(
        n for n in ast.walk(_ENGINE) if isinstance(n, ast.FunctionDef) and n.name == "_rollback"
    )
    reset = [
        n
        for n in ast.walk(func)
        if isinstance(n, ast.Compare)
        and isinstance(n.left, ast.Attribute)
        and n.left.attr == "state"
        and any(isinstance(m, ast.Attribute) and m.attr == "STATE_FAILED" for m in ast.walk(n))
    ]
    assert reset, "the rebuild reset must include failed records"


def test_snapshot_supports_the_manual_baseline_flow():
    """The "manual work between journal steps" flow: review the good steps, do
    the complex part by hand in the GUI, snapshot, let the model continue over
    MCP. objects_before must anchor on the last DONE record (the physical last
    record may be a planned tail, which never ran and carries no snapshot),
    and accept_done bundles the accepts into the same call."""
    func = next(
        n for n in ast.walk(_ENGINE) if isinstance(n, ast.FunctionDef) and n.name == "_snapshot"
    )
    assert "accept_done" in [a.arg for a in func.args.args]
    src = ast.unparse(func)
    assert "STATE_DONE" in src, "objects_before must anchor on the last done record's snapshot"
    assert "records[-1]" not in src, "a planned tail record has no snapshot to anchor on"


def _runs_inside_engine_quiet(func) -> bool:
    def _is_quiet(expr) -> bool:
        if isinstance(expr, ast.Name):
            return expr.id == "_EngineQuiet"
        if isinstance(expr, ast.Call):
            return isinstance(expr.func, ast.Name) and expr.func.id == "_EngineQuiet"
        return False

    return any(
        isinstance(n, ast.With) and any(_is_quiet(i.context_expr) for i in n.items)
        for n in ast.walk(func)
    )


def test_manual_edit_sync_ignores_engine_windows():
    """The manual-edit observer must not mirror UNDO/REDO/RE-RUN property
    writes back into the journal: undo restores the OLD value, the observer
    "manual-edited" it back over the synced params, and the re-run rebuilt
    at the old value — a user's correction silently reverted (live-verified
    as "Height 10 -> reexecute -> 6"). The observer must check the mute flag,
    and the undo/redo and re-run paths must open the window."""
    cls = next(
        n
        for n in ast.walk(_ENGINE)
        if isinstance(n, ast.ClassDef) and n.name == "_JournalSyncObserver"
    )
    slot = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "slotChangedObject"
    )
    names = {n.id for n in ast.walk(slot) if isinstance(n, ast.Name)}
    assert "_ENGINE_ACTIVE" in names, "the observer must stay silent in engine windows"
    for fname in ("_stack_op", "run_record"):
        func = next(
            n for n in ast.walk(_ENGINE) if isinstance(n, ast.FunctionDef) and n.name == fname
        )
        assert _runs_inside_engine_quiet(func), (
            f"{fname} must run its document writes inside _EngineQuiet, or undo echoes "
            "clobber the synced params"
        )
