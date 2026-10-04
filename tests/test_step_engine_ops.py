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
    assert "objects_after_index" in attrs, (
        "_rollback must verify the object set expected at the target step"
    )


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


_RPC = ast.parse((_ADDON / "rpc_server" / "rpc_server.py").read_text(encoding="utf-8"))

ASSEMBLY_JOURNAL_OPS = {"assemble", "align_shapes", "set_anchors", "assembly"}


def test_assembly_ops_are_executable_and_recorded_with_payloads():
    """Assemble/align/anchors/assembly steps used to journal with params={} and
    executable=False: rollback undid them (they own transactions) but a
    replay/rebuild SKIPPED them and the model came back unassembled, and their
    unrecoverable flag degraded every rebuild across them to "partial". They
    must be executable and carry their full payload."""
    # engine side: executable + an executor branch each
    assign = next(
        n
        for n in ast.walk(_ENGINE)
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", "") == "EXECUTABLE_OPS" for t in n.targets)
    )
    ops = {
        n.value
        for n in ast.walk(assign.value)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert ops >= ASSEMBLY_JOURNAL_OPS
    func = next(
        n for n in ast.walk(_ENGINE) if isinstance(n, ast.FunctionDef) and n.name == "_execute_one"
    )
    handled = {
        n.value
        for cmp in ast.walk(func)
        if isinstance(cmp, ast.Compare)
        for n in [cmp.left, *cmp.comparators]
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert handled >= ASSEMBLY_JOURNAL_OPS
    # RPC side: every one of the four handlers journals a NON-EMPTY params dict
    for handler in ("assemble", "align_shapes", "set_anchors", "assembly_op"):
        func = next(
            (n for n in ast.walk(_RPC) if isinstance(n, ast.FunctionDef) and n.name == handler),
            None,
        )
        assert func is not None, f"no RPC handler {handler}"
        empties = [
            d
            for f in (func,)
            for d in ast.walk(f)
            if isinstance(d, ast.Dict)
            and any(isinstance(k, ast.Constant) and k.value == "params" for k in d.keys)
            and any(isinstance(v, ast.Dict) and not v.keys for v in d.values)
        ]
        assert not empties, f"{handler} still journals params={{}} (not replayable)"


def test_empty_commit_is_downgraded_to_read_only():
    """A committed op that produced no undo entry (a read-only assembly verify,
    an edit that set the values the object already had) must not keep claiming
    a transaction — plan_rollback counts r.transaction, and a phantom claim
    pops an EARLIER step's transaction off the plain undo stack.

    The probe must be ``undo_token``, not a bare ``UndoCount`` comparison:
    FreeCAD caps the undo stack (MaxUndoSize, 20 by default), so at the cap a
    REAL commit leaves the count pinned and the journal degraded every later
    mutation to read-only — rollback/replay then silently skipped real steps.
    """
    func = next(
        n
        for n in ast.walk(_RPC)
        if isinstance(n, ast.FunctionDef) and n.name == "_run_op_with_screenshot"
    )
    attrs = {n.attr for n in ast.walk(func) if isinstance(n, ast.Attribute)}
    assert "downgrade_if_no_undo" in attrs
    assert "undo_token" in attrs, "the probe must survive FreeCAD's undo cap"
    assert "UndoCount" not in attrs, "UndoCount alone saturates at the cap"


def test_rpc_mutations_are_muted_from_the_sync_observer():
    """Machine-driven writes must not echo into earlier steps' params: the
    observer mirrors them and a later reject/replay then disagrees with
    history. Only human GUI edits sync."""
    run_op = next(
        n
        for n in ast.walk(_RPC)
        if isinstance(n, ast.FunctionDef) and n.name == "_run_op_with_screenshot"
    )
    assert any(
        isinstance(n, ast.With)
        and any("engine_quiet" in ast.unparse(i.context_expr) for i in n.items)
        for n in ast.walk(run_op)
    ), "_run_op_with_screenshot must run gui_fn inside engine_quiet()"
    execute_code = next(
        n for n in ast.walk(_RPC) if isinstance(n, ast.FunctionDef) and n.name == "execute_code"
    )
    assert any(
        isinstance(n, ast.With)
        and any("engine_quiet" in ast.unparse(i.context_expr) for i in n.items)
        for n in ast.walk(execute_code)
    ), "execute_code must run the snippet inside engine_quiet()"


def test_rollback_guards_undo_with_stack_check_and_replay_rewrites_doc_refs():
    """Two reopen-safety wirings: (a) _rollback must consult _stack_holds_journal
    before popping — after a file reopen the stack holds none of the journal, and
    for a property-only step no object moves, so blind undo pops foreign
    transactions undetected; (b) replayed snippets must go through
    _replay_ready_code, else every stored getDocument('<old name>') dies with
    "Unknown document" and the rebuild path (the only one left after a reopen)
    cannot even start — the DeskFan.FCStd/ex-MideaDeskFan bug."""
    func = next(
        n for n in ast.walk(_ENGINE) if isinstance(n, ast.FunctionDef) and n.name == "_rollback"
    )
    assert any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_stack_holds_journal"
        for n in ast.walk(func)
    ), "_rollback must verify the undo stack holds the journal's transactions"
    exec_one = next(
        n for n in ast.walk(_ENGINE) if isinstance(n, ast.FunctionDef) and n.name == "_execute_one"
    )
    assert any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_replay_ready_code"
        for n in ast.walk(exec_one)
    ), "replayed snippets must be rewritten for document renames"
