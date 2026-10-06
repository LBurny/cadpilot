"""Tool-surface metadata required by the mcp-builder guidelines.

Pins the mechanical contract: every tool carries the four MCP annotations, the
listing tools expose pagination, numeric params carry schema constraints, and
the semi-static references are reachable as resources.
"""

import ast
import asyncio
import dataclasses
from pathlib import Path

from cadpilot import server
from cadpilot.server_state import ServerState

# Tools that only read FreeCAD / local state and must advertise readOnlyHint.
_READ_ONLY = {
    "get_task_result",
    "get_view",
    "get_objects",
    "list_documents",
    "get_addon_log",
    "recall_patterns",
    "operation_help",
    "inspect_freecad",
    "measure_geometry",
    "get_topology",
    "check_interference",
    "get_positioning_info",
    "get_anchors",
    "verify_assembly",
}
# Tools that mutate the document, the step journal or the pattern store.
_MUTATING = {
    # diagnose is here because dismiss=true acts (closes a modal dialog); it is
    # the same tool as the report because the blocker is what the report names.
    "diagnose",
    "create_document",
    "cad",
    "execute_code",
    "execute_code_async",
    "session",
    "step_plan",
    "step_control",
    "save_pattern",
    "set_anchors",
    "align_shapes",
    "assemble",
    "assembly_session",
}


def _tools() -> dict:
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


def test_read_only_and_mutating_sets_cover_every_tool():
    names = set(_tools())
    assert names == _READ_ONLY | _MUTATING, names ^ (_READ_ONLY | _MUTATING)


def test_every_tool_declares_all_four_annotations():
    for name, tool in _tools().items():
        annotations = tool.annotations
        assert annotations is not None, name
        assert annotations.title, name
        for field in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
            assert getattr(annotations, field) is not None, f"{name}.{field}"


def test_annotation_kinds_match_the_tool():
    tools = _tools()
    for name in _READ_ONLY:
        assert tools[name].annotations.readOnlyHint is True, name
        assert tools[name].annotations.destructiveHint is False, name
    for name in _MUTATING:
        assert tools[name].annotations.readOnlyHint is False, name


def test_listing_tools_declare_pagination_params():
    tools = _tools()
    for name in ("get_objects", "list_documents", "session", "get_topology", "get_addon_log"):
        props = tools[name].inputSchema["properties"]
        assert "limit" in props, name
    for name in ("get_objects", "list_documents", "get_topology", "session"):
        assert "offset" in tools[name].inputSchema["properties"], name


def test_numeric_params_carry_schema_constraints():
    tools = _tools()
    limit = tools["get_topology"].inputSchema["properties"]["limit"]
    assert limit["minimum"] == 1 and limit["maximum"] == 200
    gap = tools["assembly_session"].inputSchema["properties"]["gap_samples"]
    assert gap["minimum"] == 2 and gap["maximum"] == 64
    to_step = tools["session"].inputSchema["properties"]["to_step"]
    assert to_step["anyOf"][0]["minimum"] == 0


def test_reference_docs_and_patterns_are_resources():
    resources = {str(r.uri) for r in asyncio.run(server.mcp.list_resources())}
    templates = {r.uriTemplate for r in asyncio.run(server.mcp.list_resource_templates())}
    assert "cadpilot://operations" in resources
    assert "cadpilot://patterns" in resources
    assert "cadpilot://docs/{operation}" in templates
    assert "cadpilot://patterns/{pattern_id}" in templates


def test_assemble_docs_match_the_commit_policy():
    """The addon commits the transaction whenever at least one mate passed
    (commit_if=lambda res: res.get("passed", 0) > 0, mirroring batch), so with
    stop_on_error=True the mates already applied before the failure STAY. Both
    doc surfaces claimed the opposite ("everything rolls back", "nothing
    moves"), so a caller reasoning from the docs mis-predicted the document."""
    from cadpilot import server, tool_docs

    docstring = server.assemble.__doc__ or ""
    assert "nothing moves only if the FIRST mate fails" in docstring, docstring
    assert "everything rolls back" not in docstring
    reference = tool_docs.CAD_OP_DOCS["assemble"]
    assert "ALREADY passed stay applied" in reference, reference
    assert "nothing moves only when the" in reference
    assert "aborts the whole transaction" not in reference.replace("\n", " ")


def test_server_only_touches_real_server_state_fields():
    """``diagnose(dismiss=true)`` resolved its connection with
    ``state.connection`` — the field is ``freecad_connection`` — so the
    modal-recovery action threw AttributeError. It stayed invisible because the
    test injected a working fake into the operation and never went through the
    tool layer that reads ``state``. The dataclass is the single source of
    truth, so every attribute ``server.py`` reads or writes on ``state`` is
    checked against it.

    The check is only sound while ``state`` means that one module global. The
    pin enforces the module-level assignment: exactly one, the dataclass
    itself. Any OTHER binding of the name in a function scope also fails
    loudly — an assignment trips the pin; a ``for``/``with``/parameter shadow
    makes the field check above fail on unrelated attributes instead. Read
    either failure as "rename the local", not as a ServerState bug."""
    fields = {f.name for f in dataclasses.fields(ServerState)}
    source = Path(server.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    used = {
        n.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "state"
    }
    assert used <= fields, (
        f"server.py touches ServerState attributes that do not exist: {sorted(used - fields)} "
        f"(fields: {sorted(fields)})"
    )
    assert used, "the walk found no state attribute at all — the detector is broken"
    assigns = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign))
        and any(isinstance(t, ast.Name) and t.id == "state" for t in _targets(n))
    ]
    assert len(assigns) == 1 and ast.unparse(assigns[0]).startswith("state = ServerState("), (
        f"expected exactly one module-level 'state = ServerState()', got "
        f"{[ast.unparse(a) for a in assigns]} — a rebound 'state' voids the field check"
    )


def _targets(n):
    if isinstance(n, (ast.AnnAssign, ast.AugAssign)):
        return [n.target]
    return n.targets
