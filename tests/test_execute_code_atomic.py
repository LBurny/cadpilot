"""execute_code must be rollback-able when it changes the document.

The bug this pins: a bare property write in FreeCAD creates no undo entry, so
``execute_code`` — which LLMs reach for constantly — produced journal steps that
rollback could not undo, poisoning the whole review loop. The fix wraps the
snippet in a transaction and only claims atomicity when that transaction
produced an undo entry.

The addon half imports FreeCAD and cannot be imported here, so these parse the
source with ``ast`` — same approach as ``test_addon_gui_wiring``.
"""

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ADDON = _ROOT / "addon" / "CADPilot"
_RPC = ast.parse((_ADDON / "rpc_server" / "rpc_server.py").read_text(encoding="utf-8"))
_ENGINE = ast.parse((_ADDON / "rpc_server" / "step_engine.py").read_text(encoding="utf-8"))
_CORE = ast.parse(
    (_ROOT / "src" / "cadpilot" / "operations" / "core.py").read_text(encoding="utf-8")
)


def _func(tree, name):
    return next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )


def _nested(func, name):
    return next(n for n in ast.walk(func) if isinstance(n, ast.FunctionDef) and n.name == name)


def _attr_calls(node, attr):
    """Calls of ``<anything>.<attr>(...)``, whatever the receiver is."""
    return [
        n
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr
    ]


def _dotted(node) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _strings(node) -> set[str]:
    return {
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def test_execute_code_wraps_snippet_in_a_transaction():
    body = _nested(_func(_RPC, "execute_code"), "combined_task")
    # Guarded: never open a second transaction (they do not nest), and never
    # touch one that was already pending.
    assert "HasPendingTransaction" in _strings(body) or any(
        isinstance(n, ast.Attribute) and n.attr == "HasPendingTransaction" for n in ast.walk(body)
    )
    assert _attr_calls(body, "openTransaction"), "snippet must run inside a transaction"
    assert _attr_calls(body, "abortTransaction"), "a failing snippet must roll back cleanly"
    # Atomicity is decided by whether the transaction produced an undo entry.
    assert any(
        isinstance(n, ast.Compare)
        and any(isinstance(m, ast.Attribute) and m.attr == "UndoCount" for m in ast.walk(n))
        for n in ast.walk(body)
    ), "changed must be derived from the undo count"


def test_execute_code_result_reports_changed():
    """The MCP side needs the flag to record the session step as atomic."""
    body = _func(_RPC, "execute_code")
    assert any(isinstance(n, ast.Constant) and n.value == "changed" for n in ast.walk(body))


def test_journal_step_is_atomic_and_replayable_only_when_changed():
    body = _func(_ENGINE, "append_execute_code")
    ctor = next(
        n for n in ast.walk(body) if isinstance(n, ast.Call) and _dotted(n.func) == "sj.StepRecord"
    )
    kw = {k.arg: k.value for k in ctor.keywords}
    # atomic/executable are driven by `changed`, not hard-coded.
    assert isinstance(kw.get("atomic"), ast.Name) and kw["atomic"].id == "changed"
    assert isinstance(kw.get("executable"), ast.Name) and kw["executable"].id == "changed"
    # The snippet is ALWAYS stored: the panel shows it as the step's detail (an
    # execute_code row used to render as an opaque "{}"), and replay re-runs it.
    params = kw["params"]
    assert "code" in _strings(params), "the snippet must be stored for replay"
    assert not isinstance(params, (ast.If, ast.IfExp)), "store the code unconditionally"


def test_replay_re_executes_a_recorded_snippet():
    body = _func(_ENGINE, "_execute_one")
    assert "execute_code" in _strings(body)
    assert "code" in _strings(body)
    # It must go back through the RPC module's executor to keep the namespace.
    assert any(isinstance(n, ast.ImportFrom) for n in ast.walk(body))


def test_mcp_session_step_uses_the_addon_changed_flag():
    body = _func(_CORE, "execute_code_operation")
    subs = [
        n
        for n in ast.walk(body)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
    ]
    assert any(isinstance(s.func.value, ast.Name) and s.func.value.id == "res" for s in subs), (
        "must consult the addon result"
    )
    assert any(isinstance(n, ast.Constant) and n.value == "changed" for n in ast.walk(body))
