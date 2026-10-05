"""Tests for cad() feature operations (boolean/fillet/... pattern)."""

import json

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
        fake_freecad, "boolean", "Doc", obj_properties={"op": "fuse", "tool": "Cyl"}
    )
    assert "requires obj_name" in _text(resp)
    assert fake_freecad.calls == []


def test_loft_works_without_obj_name(fake_freecad, isolated_home):
    resp = cad_operation(
        fake_freecad, "loft", "Doc", obj_properties={"profiles": ["Sketch1", "Sketch2"]}
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
        "boolean",
        "Doc",
        obj_name="Box",
        obj_properties={"op": "cut", "tool": "Cyl"},
    )
    assert sess.step_count == 1
    assert sess.steps[0].operation == "boolean"


def test_unknown_operation_lists_all(fake_freecad, isolated_home):
    resp = cad_operation(fake_freecad, "extrude", "Doc", obj_name="Box")
    text = _text(resp)
    assert "fillet" in text and "batch" in text


def test_edit_passes_expression_strings_through(fake_freecad, isolated_home):
    """Values starting with '=' are expression bindings (Spreadsheet-driven
    parametrics). The MCP side is a pure passthrough — the addon's
    property_mapper routes them to obj.setExpression."""
    cad_operation(
        fake_freecad,
        "edit_object",
        "Doc",
        obj_name="Box",
        obj_properties={"Length": "=Spreadsheet.width * 2", "Width": 30},
    )
    _, args, _ = fake_freecad.calls[0]
    assert args[0] == "Doc"
    assert args[2] == {"Properties": {"Length": "=Spreadsheet.width * 2", "Width": 30}}


def test_feature_rejects_reserved_spec_keys(fake_freecad, isolated_home):
    """obj_properties keys 'type'/'base' are internal spec keys. Letting them
    through used to clobber the spec — a pocket with {'type': 'ThroughAll'}
    died with a baffling "unknown feature type 'ThroughAll'" error instead of
    a hint at the right key."""
    resp = cad_operation(
        fake_freecad,
        "pocket",
        "Doc",
        obj_name="Sketch",
        obj_properties={"type": "ThroughAll", "length": 10},
    )
    assert "reserved" in _text(resp)
    assert fake_freecad.calls == []

    resp = cad_operation(
        fake_freecad,
        "fillet",
        "Doc",
        obj_name="Box",
        obj_properties={"base": "Box", "radius": 2},
    )
    assert "reserved" in _text(resp)
    assert fake_freecad.calls == []


def test_feature_internal_spec_keys_win_over_params(fake_freecad, isolated_home):
    """Regression guard for the dict-order bug: internal spec keys must be
    written AFTER user params so the operation type always reaches the addon."""
    cad_operation(fake_freecad, "pad", "Doc", obj_name="Sketch", obj_properties={"length": 5})
    _, args, _ = fake_freecad.calls[0]
    assert args[1]["type"] == "pad"
    assert args[1]["base"] == "Sketch"
    assert args[1]["length"] == 5


def test_pocket_warning_reaches_the_model(fake_freecad, isolated_home):
    """The addon's `warnings` used to be dropped for every non-batch op, so a
    pocket that cut AIR reported plain success (describe_feature had detected
    it). The response is the only thing the model reads."""
    fake_freecad.result_overrides["create_feature"] = {
        "success": True,
        "object_name": "Pocket",
        "warnings": ["Pocket removed no material (volume unchanged at 38603.9 mm^3): ..."],
    }
    resp = cad_operation(
        fake_freecad, "pocket", "Doc", obj_name="Sketch", obj_properties={"length": 10}
    )
    text = _text(resp)
    assert "removed no material" in text
    assert "WARNING" in text, "a silent no-op must not read as plain success"


def test_sketch_dof_reaches_the_model(fake_freecad, isolated_home):
    """operation_help promises the sketch result reports fully_constrained."""
    fake_freecad.result_overrides["create_feature"] = {
        "success": True,
        "object_name": "S1",
        "dof": 0,
        "fully_constrained": True,
    }
    resp = cad_operation(
        fake_freecad, "sketch", "Doc", obj_name="S1", obj_properties={"geometry": []}
    )
    payload = json.loads(_text(resp))
    assert payload["fully_constrained"] is True
    assert payload["dof"] == 0


def test_op_without_extras_keeps_its_plain_summary(fake_freecad, isolated_home):
    """No behavior change where nothing was wrong: the transport bookkeeping
    (objects fingerprint, transaction flag) must not leak into the reply."""
    resp = cad_operation(fake_freecad, "create_object", "Doc", obj_type="Part::Box", obj_name="B")
    text = _text(resp)
    assert text.strip() == "Object 'B' created successfully"


def test_feature_description_rides_along_in_the_spec(fake_freecad, isolated_home):
    """The addon turns spec["description"] into the Steps panel's row label,
    so the MCP layer must put it IN the payload. It used to reach only the
    MCP-side session log, which the panel never reads — the human watching
    the panel then saw "pad on 'Sketch'" no matter what the model intended."""
    cad_operation(
        fake_freecad,
        "pad",
        "Doc",
        obj_name="Sketch",
        obj_properties={"length": 6},
        description="flange body, 6mm thick",
    )
    _, args, _ = fake_freecad.calls[0]
    assert args[1] == {
        "type": "pad",
        "base": "Sketch",
        "length": 6,
        "description": "flange body, 6mm thick",
    }


def test_no_description_leaves_the_feature_spec_untouched(fake_freecad, isolated_home):
    cad_operation(fake_freecad, "pad", "Doc", obj_name="Sketch", obj_properties={"length": 6})
    _, args, _ = fake_freecad.calls[0]
    assert args[1] == {"type": "pad", "base": "Sketch", "length": 6}


def test_create_and_edit_object_carry_the_description(fake_freecad, isolated_home):
    cad_operation(
        fake_freecad,
        "create_object",
        "Doc",
        obj_type="Part::Box",
        obj_name="Box",
        obj_properties={"Length": 10},
        description="stock for the fixture",
    )
    create = next(c for c in fake_freecad.calls if c[0] == "create_object")
    assert create[1][1]["description"] == "stock for the fixture"
    assert create[1][1]["Properties"] == {"Length": 10}

    cad_operation(
        fake_freecad,
        "edit_object",
        "Doc",
        obj_name="Box",
        obj_properties={"Length": 20},
        description="thicker plate",
    )
    edit = next(c for c in fake_freecad.calls if c[0] == "edit_object")
    assert edit[1][2]["description"] == "thicker plate"
    assert edit[1][2]["Properties"] == {"Length": 20}
