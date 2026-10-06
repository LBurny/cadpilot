"""Tool-surface metadata required by the mcp-builder guidelines.

Pins the mechanical contract: every tool carries the four MCP annotations, the
listing tools expose pagination, numeric params carry schema constraints, and
the semi-static references are reachable as resources.
"""

import asyncio

from cadpilot import server

# Tools that only read FreeCAD / local state and must advertise readOnlyHint.
_READ_ONLY = {
    "get_task_result",
    "get_view",
    "get_objects",
    "list_documents",
    "get_addon_log",
    "diagnose",
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
    "create_document",
    "dismiss_blocking_dialog",
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
