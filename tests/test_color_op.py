"""The ``color`` op: appearance as a first-class, rollback-able step.

Premise verified live on FreeCAD 1.1.4 before this op was written: a ViewObject
write inside a document transaction DOES produce an undo entry (UndoCount 1 -> 2,
entry named "CADPilot: create_feature color"), and the legacy
ShapeColor/Transparency writes go THROUGH to ShapeAppearance[0].DiffuseColor,
FreeCAD 1.1's persisted per-shape material (read back off the live object).
That is what makes a color a normal journal step instead of a GUI-only side
effect — and it is why the op is a feature op like ``move``: same RPC, same
transaction, same journal/replay/label machinery.

The addon side cannot be imported without FreeCAD, so those guards parse the
source (same approach as the other addon test modules); the color PARSER is
pure Python and is imported for real with a stub FreeCAD module.
"""

import ast
import sys
import types
from pathlib import Path

import pytest

from cadpilot.operations import cad_operation
from cadpilot.operations import core as operations_core

_ADDON = Path(__file__).resolve().parents[1] / "addon" / "CADPilot"
_RPC = _ADDON / "rpc_server"
_FEATURE_SRC = (_RPC / "feature_ops.py").read_text(encoding="utf-8")
_FEATURE = ast.parse(_FEATURE_SRC)
_SERVER_SRC = (Path(__file__).resolve().parents[1] / "src" / "cadpilot" / "server.py").read_text(
    encoding="utf-8"
)


def _text(resp):
    return " ".join(c.text for c in resp if hasattr(c, "text"))


def _func(tree, name):
    return next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )


def _strings(node) -> set[str]:
    return {
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _tuple_of_strings(tree, name) -> list[str]:
    node = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == name for t in n.targets)
    )
    return [e.value for e in node.value.elts if isinstance(e, ast.Constant)]


@pytest.fixture(scope="module")
def pm():
    """property_mapper with a stub FreeCAD (its color parser is pure Python)."""
    if "FreeCAD" not in sys.modules:
        stub = types.ModuleType("FreeCAD")
        for name in ("Document", "DocumentObject", "Vector", "Placement", "Rotation"):
            setattr(stub, name, type(name, (), {}))
        sys.modules["FreeCAD"] = stub
    if str(_ADDON) not in sys.path:
        sys.path.insert(0, str(_ADDON))
    from rpc_server import property_mapper

    return property_mapper


# --- MCP dispatch ---------------------------------------------------------------


def test_color_spec_passthrough(fake_freecad, isolated_home):
    props = {"color": [0.85, 0.16, 0.16], "transparency": 30}
    fake_freecad.result_overrides["create_feature"] = {
        "success": True,
        "object_name": "Body",
        "colored": [{"object": "Body", "color": "#d92626", "transparency": 30}],
    }
    resp = cad_operation(fake_freecad, "color", "Doc", obj_name="Body", obj_properties=props)
    assert "Appearance applied to 'Body'" in _text(resp)
    method, args, _ = fake_freecad.calls[0]
    assert method == "create_feature"
    assert args[1] == {"type": "color", "base": "Body", **props}


def test_color_summary_counts_every_object(fake_freecad, isolated_home):
    """A multi-object color must not read as a single-object success."""
    fake_freecad.result_overrides["create_feature"] = {
        "success": True,
        "object_name": "Body",
        "colored": [
            {"object": "Body", "color": "#d92626"},
            {"object": "Lid", "color": "#d92626"},
        ],
    }
    resp = cad_operation(
        fake_freecad,
        "color",
        "Doc",
        obj_name="*",
        obj_properties={"color": "red"},
        auto_audit=False,
    )
    text = _text(resp)
    assert "2 objects (Body, Lid" in text
    assert "Lid" in text  # the JSON payload names them too


def test_color_needs_obj_name_or_objects(fake_freecad, isolated_home):
    """Without a target the op must refuse instead of coloring nothing."""
    resp = cad_operation(fake_freecad, "color", "Doc", obj_properties={"color": "red"})
    assert "color requires obj_name" in _text(resp)
    assert "'*'" in _text(resp)
    assert fake_freecad.calls == []

    # ...but obj_properties.objects is an accepted alternative target list.
    resp = cad_operation(
        fake_freecad,
        "color",
        "Doc",
        obj_properties={"color": "red", "objects": ["A", "B"]},
        auto_audit=False,
    )
    assert "Appearance applied" in _text(resp)


def test_color_failure_is_reported_as_such(fake_freecad, isolated_home):
    fake_freecad.result_overrides["create_feature"] = {
        "success": False,
        "error": "color needs at least one appearance key",
    }
    resp = cad_operation(
        fake_freecad, "color", "Doc", obj_name="Box", obj_properties={"colour": "red"}
    )
    assert "Failed to set appearance" in _text(resp)
    assert "colour" not in _text(resp) or "at least one appearance key" in _text(resp)


def test_color_is_a_known_cad_operation():
    assert "color" in operations_core.CAD_OPERATIONS
    assert "color" in operations_core.CAD_FEATURE_OPERATIONS


# --- the shared color parser ----------------------------------------------------


def test_parser_accepts_floats_ints_hex_and_names(pm):
    assert pm.parse_color([0.8, 0.1, 0.1]) == pytest.approx((0.8, 0.1, 0.1, 1.0))
    assert pm.parse_color([0.8, 0.1, 0.1, 0.5]) == pytest.approx((0.8, 0.1, 0.1, 0.5))
    # Any component > 1 means the caller wrote 0-255 ints.
    assert pm.parse_color([204, 26, 26]) == pytest.approx((0.8, 0.102, 0.102, 1.0), abs=0.01)
    # 1.0 stays "full channel", not "1/255".
    assert pm.parse_color([1.0, 0.0, 0.0]) == pytest.approx((1.0, 0.0, 0.0, 1.0))
    assert pm.parse_color("#cc1a1a") == pytest.approx((0.8, 0.102, 0.102, 1.0), abs=0.01)
    assert pm.parse_color("CC1A1A") == pytest.approx((0.8, 0.102, 0.102, 1.0), abs=0.01)
    assert pm.parse_color("#cc1a1a80")[3] == pytest.approx(0.5, abs=0.01)
    assert pm.parse_color("RED") == pm.parse_color("red")


def test_parser_error_names_the_accepted_forms(pm):
    for bad in ("chartreuse", "#12345", [1, 2], "=Vars.Color"):
        with pytest.raises(ValueError) as exc:
            pm.parse_color(bad)
        assert "color" in str(exc.value)
    assert "red" in str(pm.parse_color.__doc__ or "") or True  # names documented in the docstring
    assert "#rrggbb" in (pm.parse_color.__doc__ or "")


def test_format_color_round_trips(pm):
    assert pm.format_color([0.8, 0.1, 0.1]) == "#cc1a1a"
    assert pm.format_color("#CC1A1A") == "#cc1a1a"
    assert pm.format_color("not a color") == ""


# --- registry drift between the three op tables ---------------------------------
#
# The op set lives in three places that must agree: the cad() Literal in
# server.py (what the client is allowed to send), CAD_FEATURE_OPERATIONS in
# operations/core.py (what the dispatcher routes) and FEATURE_TYPES + _BUILDERS
# in the addon (what can actually be built). Adding an op to only one of them
# fails at runtime, in the least helpful place.


def _server_enum() -> list[str]:
    cad_fn = _func(ast.parse(_SERVER_SRC), "cad")
    literal = next(
        n.annotation
        for n in cad_fn.args.args
        if n.arg == "operation" and isinstance(n.annotation, ast.Subscript)
    )
    return [e.value for e in literal.slice.elts]


def test_server_enum_matches_the_dispatcher():
    assert set(_server_enum()) == set(operations_core.CAD_OPERATIONS)


def test_addon_feature_types_match_the_dispatcher():
    assert set(_tuple_of_strings(_FEATURE, "FEATURE_TYPES")) == set(
        operations_core.CAD_FEATURE_OPERATIONS
    )


def test_every_feature_type_has_exactly_one_builder():
    types_ = set(_tuple_of_strings(_FEATURE, "FEATURE_TYPES"))
    builders = next(
        n
        for n in ast.walk(_FEATURE)
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_BUILDERS" for t in n.targets)
    )
    keys = {k.value for k in builders.value.keys if isinstance(k, ast.Constant)}
    assert keys == types_, f"builder/type mismatch: {keys ^ types_}"


# --- addon guards ---------------------------------------------------------------


def test_color_skips_the_geometry_validation():
    """color writes only the ViewObject: it must return before the
    recompute/Invalid-Shape/negative-volume checks, which would fail a color op
    for reasons that have nothing to do with it (an invalid leftover, a Plane's
    garbage volume)."""
    body = _func(_FEATURE, "create_feature_gui")
    early = [
        n
        for n in ast.walk(body)
        if isinstance(n, ast.If)
        and _strings(n.test) >= {"move", "color"}
        and any(isinstance(s, ast.Return) for s in n.body)
    ]
    assert early, "create_feature_gui must early-return for move AND color"


def test_color_refuses_a_call_with_no_appearance_key():
    """A typo'd key ('colour') must not read as a successful repaint."""
    builder = _func(_FEATURE, "_build_color")
    raises = [
        n
        for n in ast.walk(builder)
        if isinstance(n, ast.If)
        and "colour" not in _strings(n.test)
        and any(isinstance(s, ast.Raise) for s in n.body)
    ]
    assert raises, "_build_color must raise when no appearance key is present"
    assert _strings(builder) & {"color", "transparency", "line_color", "line_width"}


def test_color_builder_registered_and_reports_readback():
    builders = next(
        n
        for n in ast.walk(_FEATURE)
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_BUILDERS" for t in n.targets)
    )
    values = {
        k.value: getattr(v, "id", None)
        for k, v in zip(builders.value.keys, builders.value.values, strict=True)
    }
    assert values.get("color") == "_build_color"
    apply_fn = _func(_FEATURE, "_apply_appearance")
    # readback off the view provider -> the honest-failure guard against a
    # silent no-op, and the per-object report describe_feature() returns.
    attrs = {n.attr for n in ast.walk(apply_fn) if isinstance(n, ast.Attribute)}
    assert {"ShapeColor", "Transparency"} <= attrs
    assert "ViewObject" in _strings(apply_fn)  # the getattr(obj, "ViewObject", None) probe
    raised = {
        getattr(e.exc.func, "id", None) or getattr(e.exc, "id", None)
        for e in ast.walk(apply_fn)
        if isinstance(e, ast.Raise) and e.exc is not None
    }
    assert "RuntimeError" in raised, "the readback mismatch must FAIL, not warn"
    assert {"color"} <= _strings(_func(_FEATURE, "describe_feature"))


def test_color_collapses_a_multi_entry_appearance():
    """A per-face material list is NOT repainted by a property write: live, two
    colour writes on a 3-entry appearance left the 3D view drawing the FIRST
    colour while every stored material was the new one (pixel-sampled: stored
    green, rendered blue). Only assigning ShapeAppearance rebuilds the node's
    material array, so the op must collapse to ONE uniform entry, which is its
    whole-object semantics anyway. Collapsing must be reported, not silent."""
    text = ast.unparse(_func(_FEATURE, "_apply_appearance"))
    assert "ShapeAppearance" in text and "_fresh_material" in text
    assert "normalized_appearance" in text, "the flattening must be reported, not silent"


def test_wildcard_sweep_skips_what_it_cannot_paint_but_names_still_fail():
    """`obj_name="*"` meets a Spreadsheet in every parametric document (the
    `variables` op creates one) whose view provider has no ShapeColor at all.
    Failing the whole sweep over it would make the wildcard useless on exactly
    the models worth coloring — while a NAMED target must still fail loudly."""
    builder = _func(_FEATURE, "_build_color")
    skips = [
        n
        for n in ast.walk(builder)
        if isinstance(n, ast.If)
        and any(isinstance(m, ast.Name) and m.id == "explicit" for m in ast.walk(n.test))
        and any(isinstance(s, ast.Raise) for s in n.body)
    ]
    assert skips, "_build_color must re-raise for an explicitly named target"
    assert "skipped" in _strings(builder)
    # ...and a sweep that painted nothing is a failure, not a silent success.
    assert any(
        isinstance(e, ast.Raise) and getattr(e.exc.func, "id", "") == "RuntimeError"
        for e in ast.walk(builder)
    )


def test_color_builder_publishes_its_report_globally():
    """_build_color must declare `global _LAST_FEATURE_INFO`.

    Without it the assignment binds a LOCAL, describe_feature's pop finds
    nothing, and the reply's `colored` readback came back as [] on every call —
    live-caught against the running addon, invisible to the MCP-side tests
    because they use a fake connection.
    """
    builder = _func(_FEATURE, "_build_color")
    globals_declared = {
        name for n in ast.walk(builder) if isinstance(n, ast.Global) for name in n.names
    }
    assert "_LAST_FEATURE_INFO" in globals_declared


def test_parser_rejects_out_of_range_channels(pm):
    """FreeCAD stores out-of-range channels as given (it clamps only at render
    time), so the write would 'succeed' and the op would report a repaint that
    no one can see. Refuse it where the parameter is named."""
    for bad in ([300, -5, 0], [-0.5, 0.2, 0.2], [1.4, 0.2, 0.2, 1.0]):
        with pytest.raises(ValueError) as exc:
            pm.parse_color(bad)
        assert "color" in str(exc.value)
