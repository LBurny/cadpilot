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
