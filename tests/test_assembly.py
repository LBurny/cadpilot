"""Tests for the assembly toolchain operations (anchors / assemble / verify)."""

import json

from cadpilot.operations import (
    assemble_operation,
    get_anchors_operation,
    set_anchors_operation,
    verify_assembly_operation,
)


def _json(resp):
    return json.loads(resp[0].text)


# --- get_anchors -----------------------------------------------------------


def test_get_anchors_returns_json(fake_freecad):
    resp = get_anchors_operation(fake_freecad, "Doc", "Hub")
    assert fake_freecad.called_methods() == ["get_anchors"]
    data = _json(resp)
    assert data["anchors"]["axle"]["source"] == "explicit"
    assert data["anchors"]["bbox_center"]["pos"] == [5, 5, 5]


def test_get_anchors_exception_is_caught(fake_freecad):
    fake_freecad.errors["get_anchors"] = ConnectionResetError("reset")
    resp = get_anchors_operation(fake_freecad, "Doc", "Hub")
    assert "Failed to get anchors" in resp[0].text


# --- set_anchors -----------------------------------------------------------


def test_set_anchors_passes_args_and_no_screenshot_by_default(fake_freecad):
    anchors = {"rear_dropout": {"pos": [-430, 0, 70], "dir": [0, 1, 0]}}
    resp = set_anchors_operation(fake_freecad, False, "Doc", "Frame", anchors)
    _, args, _ = fake_freecad.calls[0]
    assert args[:5] == ("Doc", "Frame", anchors, False, "local")
    assert args[5] is None  # screenshot=None when with_screenshot=False
    data = _json(resp)
    assert data["success"] is True
    assert data["anchor_count"] == 1


def test_set_anchors_passes_global_coord_frame(fake_freecad):
    anchors = {"rear_dropout": {"pos": [-430, 0, 70]}}
    set_anchors_operation(fake_freecad, False, "Doc", "Frame", anchors, coord_frame="global")
    _, args, _ = fake_freecad.calls[0]
    assert args[4] == "global"


def test_set_anchors_records_session_step(fake_freecad, isolated_home):
    from cadpilot.session_state import get_current_session, new_session, set_current_session

    set_current_session(new_session("S", "Doc"))
    resp = set_anchors_operation(fake_freecad, False, "Doc", "Frame", {"p": {"pos": [0, 0, 0]}})
    assert get_current_session().step_count == 1
    assert "step #1 of session 'S'" in _json(resp)["summary"]


def test_set_anchors_rejects_empty_anchors(fake_freecad):
    resp = set_anchors_operation(fake_freecad, False, "Doc", "Frame", {})
    assert "non-empty anchors dict" in resp[0].text
    assert fake_freecad.calls == []  # validation happens before any RPC


def test_set_anchors_exception_is_caught(fake_freecad):
    fake_freecad.errors["set_anchors"] = RuntimeError("boom")
    resp = set_anchors_operation(fake_freecad, False, "Doc", "Frame", {"p": {"pos": [0, 0, 0]}})
    assert "Failed to set anchors" in resp[0].text


# --- assemble --------------------------------------------------------------


def test_assemble_passes_mates_and_returns_residuals(fake_freecad):
    mates = [
        {
            "obj": "Wheel",
            "anchor": "axis_mid",
            "target": "Frame",
            "target_anchor": "rear_dropout",
            "mode": "axis",
        }
    ]
    resp = assemble_operation(fake_freecad, False, "Doc", mates)
    _, args, _ = fake_freecad.calls[0]
    assert args == ("Doc", mates, 0.1, True, None)
    data = _json(resp)
    assert data["passed"] == 1
    assert data["mates"][0]["residual_mm"] == 0.0


def test_assemble_partial_failure_is_reported(fake_freecad):
    fake_freecad.result_overrides["assemble"] = {
        "success": False,
        "passed": 0,
        "failed": 1,
        "mates": [
            {
                "obj": "Wheel",
                "anchor": "axis_mid",
                "passed": False,
                "residual_mm": 25.0,
                "error": "residual 25.0mm exceeds tolerance 0.1mm",
            }
        ],
    }
    data = _json(
        assemble_operation(
            fake_freecad,
            False,
            "Doc",
            [{"obj": "Wheel", "anchor": "a", "target": "F", "target_anchor": "b"}],
        )
    )
    assert data["success"] is False
    assert data["mates"][0]["residual_mm"] == 25.0


def test_assemble_records_step_only_when_committed(fake_freecad, isolated_home):
    from cadpilot.session_state import get_current_session, new_session, set_current_session

    set_current_session(new_session("S", "Doc"))
    # failure override: nothing passed -> no transaction -> no step
    fake_freecad.result_overrides["assemble"] = {
        "success": False,
        "passed": 0,
        "failed": 1,
        "mates": [],
    }
    assemble_operation(
        fake_freecad,
        False,
        "Doc",
        [{"obj": "W", "anchor": "a", "target": "F", "target_anchor": "b"}],
    )
    assert get_current_session().step_count == 0


def test_assemble_exception_is_caught(fake_freecad):
    fake_freecad.errors["assemble"] = ConnectionResetError("reset")
    resp = assemble_operation(
        fake_freecad,
        False,
        "Doc",
        [{"obj": "W", "anchor": "a", "target": "F", "target_anchor": "b"}],
    )
    assert "Failed to assemble" in resp[0].text


def test_assemble_rejects_empty_mates(fake_freecad):
    resp = assemble_operation(fake_freecad, False, "Doc", [])
    assert "non-empty mates list" in resp[0].text
    assert fake_freecad.calls == []  # validation happens before any RPC


# --- verify_assembly -------------------------------------------------------


def test_verify_assembly_defaults(fake_freecad):
    resp = verify_assembly_operation(fake_freecad, "Doc")
    _, args, _ = fake_freecad.calls[0]
    assert args == ("Doc", None, 1.0, 1.0)
    data = _json(resp)
    assert data["summary"]["floating_count"] == 0


def test_verify_assembly_with_checks(fake_freecad):
    checks = [{"obj": "A", "anchor": "p", "target": "B", "target_anchor": "q"}]
    data = _json(verify_assembly_operation(fake_freecad, "Doc", checks))
    assert data["checks"][0]["passed"] is True


def test_verify_assembly_exception_is_caught(fake_freecad):
    fake_freecad.errors["verify_assembly"] = RuntimeError("boom")
    resp = verify_assembly_operation(fake_freecad, "Doc")
    assert "Failed to verify assembly" in resp[0].text
