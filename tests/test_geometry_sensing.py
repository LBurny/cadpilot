"""Tests for the geometry-sensing tools (measure / topology / interference)."""

import json

from cadpilot.operations import (
    check_interference_operation,
    get_topology_operation,
    measure_geometry_operation,
)


def _json(resp):
    return json.loads(resp[0].text)


def test_measure_geometry_returns_json(fake_freecad):
    resp = measure_geometry_operation(fake_freecad, "Doc", "Box")
    assert fake_freecad.called_methods() == ["measure_geometry"]
    data = _json(resp)
    assert data["volume_mm3"] == 1000.0
    assert data["is_valid"] is True
    assert data["counts"]["faces"] == 6


def test_measure_geometry_error_propagates(fake_freecad):
    fake_freecad.result_overrides["measure_geometry"] = {
        "success": False,
        "error": "Object 'Box' has no Shape.",
    }
    data = _json(measure_geometry_operation(fake_freecad, "Doc", "Box"))
    assert data["success"] is False
    assert "no Shape" in data["error"]


def test_measure_geometry_exception_is_caught(fake_freecad):
    fake_freecad.errors["measure_geometry"] = ConnectionResetError("reset")
    resp = measure_geometry_operation(fake_freecad, "Doc", "Box")
    assert "Failed to measure geometry" in resp[0].text


def test_get_topology_passes_pagination(fake_freecad):
    resp = get_topology_operation(fake_freecad, "Doc", "Box", "edges", 20, 40)
    _, args, _ = fake_freecad.calls[0]
    assert args == ("Doc", "Box", "edges", 20, 40)
    data = _json(resp)
    assert data["element"] == "edges"
    assert data["edges"][0]["type"] == "Plane"


def test_get_topology_defaults(fake_freecad):
    get_topology_operation(fake_freecad, "Doc", "Box")
    _, args, _ = fake_freecad.calls[0]
    assert args == ("Doc", "Box", "faces", 50, 0)


def test_check_interference_returns_json(fake_freecad):
    resp = check_interference_operation(fake_freecad, "Doc", "A", "B")
    assert fake_freecad.called_methods() == ["check_interference"]
    data = _json(resp)
    assert data["intersects"] is True
    assert data["common_volume_mm3"] == 12.5


def test_check_interference_exception_is_caught(fake_freecad):
    fake_freecad.errors["check_interference"] = RuntimeError("boom")
    resp = check_interference_operation(fake_freecad, "Doc", "A", "B")
    assert "Failed to check interference" in resp[0].text
