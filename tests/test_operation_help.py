"""Tests for operation_help (on-demand reference docs) and the docstring budget."""

import ast
import inspect
from pathlib import Path

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
    for op in ("sketch", "hull", "datum_plane", "assembly_session", "assemble", "screenshots"):
        assert op in text


def test_help_session_topic_covers_actions():
    text = _text(operation_help_operation("session"))
    assert "rollback" in text and "to_step" in text and "complete" in text


def test_help_screenshots_topic_explains_file_delivery():
    text = _text(operation_help_operation("screenshots"))
    # File-only delivery: where the PNG lands and how the client views it.
    assert "screenshots/" in text and "path" in text and "file-reading" in text


def test_operation_help_tool_registered():
    from cadpilot import server

    resp = server.operation_help(None, operation="hull")  # ctx injected at runtime
    assert "sketches" in _text(resp)


def test_every_feature_type_is_documented_and_discoverable():
    """The addon's ``FEATURE_TYPES``, the on-demand reference and the overview
    listing are three surfaces of one interface, written by hand. An operation
    that exists in the builder table but not in the reference is undiscoverable:
    the caller reads the docs, finds nothing, and guesses — or reinvents the
    operation badly out of primitives.

    The reverse direction (a reference for an op nothing implements) is pinned
    by the explicit _NON_FEATURE_TOPICS inventory below: a doc entry for an op
    that left FEATURE_TYPES can only mean a stale reference.

    The overview assertion is deliberately weak and worth saying so: the
    overview listing is GENERATED from CAD_OP_DOCS, so "every documented topic
    is listed" guards that generator, not a hand-maintained copy.

    Scope note: this pins the OPERATION surface. ``through_all`` is the
    standing warning for the PARAMETER surface — a key the builder ignores and
    no document mentions, so "make it go through" was silently a 10 mm blind
    hole and the caller had no way to know either half. Parameter coverage is
    the next test down.

    This joins the wire-contract checks in ``test_rpc_contract.py``: every
    interface this project publishes is written down more than once, and the
    copies are compared by a test rather than by whoever edits one of them.
    """
    from cadpilot import tool_docs

    feature_types, _ = _feature_ops_source()
    ops = [e.value for e in feature_types.elts]
    assert len(ops) > 10, ops  # the extraction found the tuple, not a fragment

    assert set(ops) <= set(tool_docs.CAD_OP_DOCS), (
        f"feature ops with no operation_help reference: "
        f"{sorted(set(ops) - set(tool_docs.CAD_OP_DOCS))}"
    )
    extra = set(tool_docs.CAD_OP_DOCS) - set(ops)
    stale = extra - _NON_FEATURE_TOPICS
    assert not stale, (
        f"documented topics that are neither a FEATURE_TYPE nor on the "
        f"non-feature inventory: {sorted(stale)} — a reference for an op "
        "nothing implements is the mirror of an undocumented op"
    )
    overview = _text(operation_help_operation(None))
    missing = [op for op in tool_docs.CAD_OP_DOCS if op not in overview]
    assert not missing, f"documented but not listed in the overview: {missing}"


# Every CAD_OP_DOCS key that is not a feature op, named outright. assemble and
# assembly_session are the anchor-based pair, batch wraps several ops, the
# object/session/step topics serve their own tools, multi_agent and screenshots
# are guidance, diagnose and get_addon_log are the debugging pair.
_NON_FEATURE_TOPICS = {
    "assemble",
    "assembly_session",
    "batch",
    "create_object",
    "delete_object",
    "diagnose",
    "edit_object",
    "get_addon_log",
    "multi_agent",
    "screenshots",
    "session",
    "step_control",
    "step_labels",
    "step_plan",
}


def test_every_builder_parameter_is_documented():
    """The parameter-level half of the through_all lesson: a key a builder
    actually reads must be described in that operation's reference. A key that
    is honoured but undocumented is invisible to the caller, who then either
    never uses the feature or reaches for a numeric stand-in (a big ``length``
    where ``through_all`` existed) that turns into silent wrong geometry the
    first time a dimension changes.

    The reads are collected from the builder source, following the delegation
    the builder table uses (``pad`` is a plain function over ``_build_padlike``,
    ``fillet`` a lambda over ``_build_fillet_chamfer``), so a parameter read
    through a delegate still counts; delegates into OTHER addon modules (sketch
    builds through sketcher_ops) are not followed, so those ops under-report.
    Because the follower also walks helpers that merely RECEIVE the spec (like
    ``_require``), it can pick up keys that are not caller-facing at all —
    those live in _INTERNAL_SPEC_KEYS with their reason, and a new one must be
    added consciously rather than by silencing the test. For keys that ARE
    caller-facing the check is one-directional: it greps the reference for the
    key name, so prose that mentions the parameter passes and only a truly
    undescribed key fails.
    """
    from cadpilot import tool_docs

    _, builders = _feature_ops_source()
    for op, keys in sorted(_builder_spec_reads(builders).items()):
        doc = tool_docs.CAD_OP_DOCS.get(op, "")
        undocumented = sorted(k for k in keys if k not in _INTERNAL_SPEC_KEYS and k not in doc)
        assert not undocumented, (
            f"cad(operation={op!r}) reads spec keys {undocumented} that its "
            "operation_help reference never mentions — document them or drop "
            "the read"
        )


# Keys the addon reads off the spec that the CALLER never passes: core.py
# builds them at the tool boundary. ``base`` carries obj_name (and only for the
# CAD_NO_BASE_OPERATIONS), ``type`` carries the operation name; ``_require``
# also reads ``type``, but only to name the op in its error message.
_INTERNAL_SPEC_KEYS = {"base", "type"}


_FEATURE_OPS = (
    Path(__file__).resolve().parents[1] / "addon" / "CADPilot" / "rpc_server" / "feature_ops.py"
)


def _feature_ops_source():
    """(FEATURE_TYPES tuple node, _BUILDERS dict) parsed from the addon."""
    tree = ast.parse(_FEATURE_OPS.read_text(encoding="utf-8"))
    feature_types = next(
        n.value
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", None) == "FEATURE_TYPES" for t in n.targets)
    )
    builders = next(
        n.value
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", None) == "_BUILDERS" for t in n.targets)
    )
    return feature_types, {k.value: v for k, v in zip(builders.keys, builders.values, strict=True)}


def _builder_spec_reads(builders) -> dict:
    """op -> the set of spec keys its builder (or its delegate) reads.

    Walks the builder body for ``spec[...]`` / ``spec.get(...)`` reads, follows
    one kind of indirection — a delegation call (a lambda body or a
    ``return _build_x(doc, spec, ...)``) whose arguments carry the spec — and
    also tracks local rebindings of the spec dict. Anything it cannot resolve
    is simply not collected: the guard is allowed to under-report.
    """
    tree = ast.parse(_FEATURE_OPS.read_text(encoding="utf-8"))
    funcs = {f.name: f for f in tree.body if isinstance(f, ast.FunctionDef)}
    out: dict = {}

    def scan(node, spec_names, depth):
        keys: set = set()
        for n in ast.walk(node):
            recv = None
            if (
                isinstance(n, ast.Subscript)
                and isinstance(n.value, ast.Name)
                and n.value.id in spec_names
                and isinstance(n.slice, ast.Constant)
            ):
                keys.add(n.slice.value)
                continue
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "get"
                and isinstance(n.func.value, ast.Name)
            ):
                recv = n.func.value.id
                if recv in spec_names and n.args and isinstance(n.args[0], ast.Constant):
                    keys.add(n.args[0].value)
                    continue
            # x = spec: a plain-name alias reads as spec too. Aliases of
            # spec.get(...) SUBDICTS are deliberately not followed: their keys
            # belong to a nested structure with its own docs, and folding them
            # into the flat key set would invite false alarms.
            if (
                isinstance(n, ast.Assign)
                and isinstance(n.value, ast.Name)
                and n.value.id in spec_names
            ):
                spec_names |= {t.id for t in n.targets if isinstance(t, ast.Name)}
            # Delegation: _build_x(doc, spec, ...) — follow the callee.
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id in funcs
                and depth < 4
            ):
                params = [a.arg for a in funcs[n.func.id].args.args]
                for i, arg in enumerate(n.args):
                    if isinstance(arg, ast.Name) and arg.id in spec_names and i < len(params):
                        keys |= scan(funcs[n.func.id], {params[i]}, depth + 1)
        return keys

    for op, target in builders.items():
        if isinstance(target, ast.Lambda):
            out[op] = scan(target.body, {a.arg for a in target.args.args}, 0)
        elif isinstance(target, ast.Name) and target.id in funcs:
            out[op] = scan(funcs[target.id], {"spec"}, 0)
    return out


# Calibrated at the tool-surface consolidation pass (~8477 chars in use at 27
# tools): screenshots moved to get_view only (no more per-tool screenshot
# params), get_object folded into get_objects, and the ten session_* tools
# collapsed into one session(action=...) dispatcher. ELASTIC by user
# decision: an added tool may raise the budget by ~150 chars (100-200 range),
# so the limit scales with the tool count instead of punishing growth with a
# constant cap. The detailed reference still belongs in tool_docs.py (served
# on demand via operation_help), not in docstrings that get injected into the
# client's context with every tools/list response.
BUDGET_BASE = 9000
BUDGET_REF_TOOLS = 27
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
    limit = BUDGET_BASE + BUDGET_PER_TOOL * max(0, tools - BUDGET_REF_TOOLS)
    assert total < limit, (
        f"tool docstrings total {total} chars (limit {limit} for {tools} tools); "
        f"biggest: {biggest[:5]} — move reference text to tool_docs.py"
    )
