"""Tests for cad() sketch/PartDesign feature operations."""

from cadpilot.operations import cad_operation, session_start_operation
from cadpilot.session_state import get_current_session


def _text(resp):
    return " ".join(c.text for c in resp if hasattr(c, "text"))


def test_sketch_spec_passthrough(fake_freecad, isolated_home):
    props = {
        "plane": "XY",
        "geometry": [{"type": "line", "from": [0, 0], "to": [10, 0]}],
        "constraints": [{"type": "horizontal", "items": [[0]]}],
    }
    resp = cad_operation(
        fake_freecad, False, "sketch", "Doc", obj_name="Profile", obj_properties=props
    )
    assert "created successfully" in _text(resp)
    _, args, _ = fake_freecad.calls[0]
    assert args[1] == {"type": "sketch", "base": "Profile", **props}


def test_sketch_and_variables_need_no_obj_name(fake_freecad, isolated_home):
    r1 = cad_operation(
        fake_freecad,
        False,
        "sketch",
        "Doc",
        obj_properties={"plane": "XY", "geometry": []},
        auto_audit=False,
    )
    r2 = cad_operation(
        fake_freecad,
        False,
        "variables",
        "Doc",
        obj_properties={"cells": {"A1": ["width", 40]}},
        auto_audit=False,
    )
    assert "created successfully" in _text(r1)
    assert "created successfully" in _text(r2)
    _, args1, _ = fake_freecad.calls[0]
    _, args2, _ = fake_freecad.calls[1]
    assert args1[1]["base"] is None
    assert args2[1] == {"type": "variables", "base": None, "cells": {"A1": ["width", 40]}}


def test_pad_requires_sketch_obj_name(fake_freecad, isolated_home):
    resp = cad_operation(fake_freecad, False, "pad", "Doc", obj_properties={"length": 10})
    assert "requires obj_name" in _text(resp)
    assert fake_freecad.calls == []


def test_new_ops_record_session_steps(fake_freecad, isolated_home):
    fake_freecad.documents = ["Doc"]
    session_start_operation(fake_freecad, "Doc", "sketch-demo")
    sess = get_current_session()
    cad_operation(
        fake_freecad,
        False,
        "sketch",
        "Doc",
        obj_name="S1",
        obj_properties={"plane": "XY", "geometry": []},
    )
    cad_operation(
        fake_freecad,
        False,
        "pad",
        "Doc",
        obj_name="S1",
        obj_properties={"length": "=Spreadsheet.thickness"},
    )
    assert sess.step_count == 2
    assert [s.operation for s in sess.steps] == ["sketch", "pad"]


def test_all_new_ops_in_feature_set(fake_freecad, isolated_home):
    for op in (
        "variables",
        "sketch",
        "pad",
        "pocket",
        "revolution",
        "groove",
        "thickness",
        "draft",
    ):
        resp = cad_operation(fake_freecad, False, op, "Doc", obj_name="Base", obj_properties={})
        assert "Unknown cad operation" not in _text(resp), op


def test_datum_plane_spec_passthrough(fake_freecad, isolated_home):
    resp = cad_operation(
        fake_freecad,
        False,
        "datum_plane",
        "Doc",
        obj_name="DP1",
        obj_properties={"plane": "XY", "offset": 20},
    )
    assert "created successfully" in _text(resp)
    _, args, _ = fake_freecad.calls[0]
    assert args[1] == {"type": "datum_plane", "base": "DP1", "plane": "XY", "offset": 20}


def test_datum_plane_needs_no_obj_name(fake_freecad, isolated_home):
    resp = cad_operation(
        fake_freecad, False, "datum_plane", "Doc", obj_properties={"plane": "XZ"}, auto_audit=False
    )
    assert "created successfully" in _text(resp)
    _, args, _ = fake_freecad.calls[0]
    assert args[1]["base"] is None


def test_hull_spec_passthrough(fake_freecad, isolated_home):
    resp = cad_operation(
        fake_freecad,
        False,
        "hull",
        "Doc",
        obj_name="Body",
        obj_properties={"sketches": ["S_Top", "S_Front"]},
    )
    assert "created successfully" in _text(resp)
    _, args, _ = fake_freecad.calls[0]
    assert args[1] == {"type": "hull", "base": "Body", "sketches": ["S_Top", "S_Front"]}


def test_hull_needs_no_obj_name(fake_freecad, isolated_home):
    resp = cad_operation(
        fake_freecad,
        False,
        "hull",
        "Doc",
        obj_properties={"sketches": {"top": "A", "front": "B"}},
        auto_audit=False,
    )
    assert "created successfully" in _text(resp)
    _, args, _ = fake_freecad.calls[0]
    assert args[1]["base"] is None
