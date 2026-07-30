"""Tests for cad() feature operations (boolean/fillet/... pattern)."""

from cadpilot.operations import cad_operation, session_start_operation
from cadpilot.session_state import get_current_session


def _text(resp):
    return " ".join(c.text for c in resp if hasattr(c, "text"))


def _start_session(fake_freecad):
    fake_freecad.documents = ["Doc"]
    session_start_operation(fake_freecad, "Doc", "feat-demo")
    return get_current_session()


def test_feature_spec_assembly(fake_freecad, isolated_home):
    resp = cad_operation(
        fake_freecad,
        False,
        "fillet",
        "Doc",
        obj_name="Box",
        obj_properties={"edges": [0, 2], "radius": 2},
    )
    assert "created successfully" in _text(resp)
    _, args, _ = fake_freecad.calls[0]
    assert args[0] == "Doc"
    assert args[1] == {"type": "fillet", "base": "Box", "edges": [0, 2], "radius": 2}


def test_feature_requires_obj_name(fake_freecad, isolated_home):
    resp = cad_operation(
        fake_freecad, False, "boolean", "Doc", obj_properties={"op": "fuse", "tool": "Cyl"}
    )
    assert "requires obj_name" in _text(resp)
    assert fake_freecad.calls == []


def test_loft_works_without_obj_name(fake_freecad, isolated_home):
    resp = cad_operation(
        fake_freecad, False, "loft", "Doc", obj_properties={"profiles": ["Sketch1", "Sketch2"]}
    )
    _, args, _ = fake_freecad.calls[0]
    assert args[1]["base"] is None
    assert "created successfully" in _text(resp)


def test_feature_error_propagates(fake_freecad, isolated_home):
    fake_freecad.result_overrides["create_feature"] = {
        "success": False,
        "error": "Edge index 9 out of range (0-11).",
    }
    resp = cad_operation(
        fake_freecad,
        False,
        "fillet",
        "Doc",
        obj_name="Box",
        obj_properties={"edges": [9], "radius": 2},
    )
    assert "out of range" in _text(resp)


def test_feature_records_session_step(fake_freecad, isolated_home):
    sess = _start_session(fake_freecad)
    cad_operation(
        fake_freecad,
        False,
        "boolean",
        "Doc",
        obj_name="Box",
        obj_properties={"op": "cut", "tool": "Cyl"},
    )
    assert sess.step_count == 1
    assert sess.steps[0].operation == "boolean"


def test_unknown_operation_lists_all(fake_freecad, isolated_home):
    resp = cad_operation(fake_freecad, False, "extrude", "Doc", obj_name="Box")
    text = _text(resp)
    assert "fillet" in text and "batch" in text


def test_edit_passes_expression_strings_through(fake_freecad, isolated_home):
    """Values starting with '=' are expression bindings (Spreadsheet-driven
    parametrics). The MCP side is a pure passthrough — the addon's
    property_mapper routes them to obj.setExpression."""
    cad_operation(
        fake_freecad,
        False,
        "edit_object",
        "Doc",
        obj_name="Box",
        obj_properties={"Length": "=Spreadsheet.width * 2", "Width": 30},
    )
    _, args, _ = fake_freecad.calls[0]
    assert args[0] == "Doc"
    assert args[2] == {"Properties": {"Length": "=Spreadsheet.width * 2", "Width": 30}}
