"""Tests for operation_help (on-demand reference docs) and the docstring budget."""

import ast
import inspect

from cadpilot.operations import operation_help_operation


def _text(resp):
    return " ".join(c.text for c in resp if hasattr(c, "text"))


def test_help_returns_full_sketch_reference():
    text = _text(operation_help_operation("sketch"))
    assert "GeoId" in text and "constraints" in text and "external" in text


def test_help_covers_new_ops():
    for op in ("hull", "datum_plane", "pad", "boolean"):
        assert len(_text(operation_help_operation(op))) > 100, op


def test_help_assembly_session_topic():
    text = _text(operation_help_operation("assembly_session"))
    assert "joint_type" in text and "rollback" in text and "trim" in text


def test_help_unknown_op_lists_available():
    text = _text(operation_help_operation("nonexistent_op"))
    assert "unknown operation" in text and "hull" in text


def test_help_overview_lists_all_topics():
    text = _text(operation_help_operation(None))
    for op in ("sketch", "hull", "datum_plane", "assembly_session", "assemble"):
        assert op in text


def test_operation_help_tool_registered():
    from cadpilot import server

    resp = server.operation_help(None, operation="hull")  # ctx injected at runtime
    assert "sketches" in _text(resp)


# Calibrated at the file-mode-default change (~11060 chars in use at 37 tools;
# marking "file" as the default screenshot mode in every tool docstring grew
# the total past the old 11000 base, and clear per-tool semantics won over the
# budget). ELASTIC by user decision: an added tool may raise the budget by
# ~150 chars (100-200 range), so the limit scales with the tool count instead
# of punishing growth with a constant cap. The detailed reference still
# belongs in tool_docs.py (served on demand via operation_help), not in
# docstrings that get injected into the client's context with every tools/list
# response.
BUDGET_REF_TOOLS = 37
BUDGET_PER_TOOL = 150


def test_tool_docstring_budget():
    """Tool docstrings stay proportional to the tool count, not unbounded."""
    from cadpilot import server

    tree = ast.parse(inspect.getsource(server))
    total = 0
    biggest: list[tuple[int, str]] = []
    tools = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and getattr(dec.func, "attr", "") == "tool":
                    doc = ast.get_docstring(node) or ""
                    total += len(doc)
                    biggest.append((len(doc), node.name))
                    tools += 1
    biggest.sort(reverse=True)
    limit = 11100 + BUDGET_PER_TOOL * max(0, tools - BUDGET_REF_TOOLS)
    assert total < limit, (
        f"tool docstrings total {total} chars (limit {limit} for {tools} tools); "
        f"biggest: {biggest[:5]} — move reference text to tool_docs.py"
    )
