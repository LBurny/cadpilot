"""Tests for cadpilot.operations.core using the fake connection.

The second positional argument of the operations is ``with_screenshot``:
True = attach a screenshot to the response, False = text only.
"""

import json

from mcp.types import ImageContent, TextContent

from cadpilot.operations import (
    align_shapes_operation,
    cad_operation,
    create_document_operation,
    execute_code_async_operation,
    execute_code_operation,
    execute_operations_operation,
    get_object_operation,
    get_objects_operation,
    get_positioning_info_operation,
    get_task_result_operation,
    get_view_operation,
    list_documents_operation,
)


def _text(resp) -> str:
    return " ".join(c.text for c in resp if isinstance(c, TextContent))


def _has_image(resp) -> bool:
    return any(isinstance(c, ImageContent) for c in resp)


# --- create_object (via cad) ------------------------------------------------


def test_create_object_with_screenshot_single_rpc(fake_freecad):
    resp = cad_operation(
        fake_freecad,
        True,
        "create_object",
        "Doc",
        obj_type="Part::Box",
        obj_name="Box",
        auto_audit=False,
    )
    # exactly one RPC; screenshot is requested inline, no second call
    assert fake_freecad.called_methods() == ["create_object"]
    _, args, _ = fake_freecad.calls[0]
    assert args[2] == {"view_name": "Isometric"}  # screenshot params
    assert "created successfully" in _text(resp)
    assert _has_image(resp)


def test_create_object_without_screenshot_is_text_only(fake_freecad):
    resp = cad_operation(
        fake_freecad, False, "create_object", "Doc", obj_type="Part::Box", obj_name="Box"
    )
    _, args, _ = fake_freecad.calls[0]
    assert args[2] is None  # no screenshot requested
    assert not _has_image(resp)


def test_create_object_failure_reports_error(fake_freecad):
    fake_freecad.result_overrides["create_object"] = {"success": False, "error": "boom"}
    resp = cad_operation(
        fake_freecad, True, "create_object", "Doc", obj_type="Part::Box", obj_name="Box"
    )
    assert "boom" in _text(resp)
    assert not _has_image(resp)


def test_create_object_exception_is_caught(fake_freecad):
    fake_freecad.errors["create_object"] = ConnectionResetError("reset")
    resp = cad_operation(
        fake_freecad, True, "create_object", "Doc", obj_type="Part::Box", obj_name="Box"
    )
    assert "cad create_object failed" in _text(resp)


# --- edit / delete (via cad) ------------------------------------------------


def test_edit_object_with_screenshot_single_rpc(fake_freecad):
    resp = cad_operation(
        fake_freecad,
        True,
        "edit_object",
        "Doc",
        obj_name="Box",
        obj_properties={"Length": 5},
        auto_audit=False,
    )
    assert fake_freecad.called_methods() == ["edit_object"]
    _, args, _ = fake_freecad.calls[0]
    assert args[2] == {"Properties": {"Length": 5}}
    assert args[3] == {"view_name": "Isometric"}
    assert _has_image(resp)


def test_edit_object_failure_no_screenshot(fake_freecad):
    fake_freecad.result_overrides["edit_object"] = {"success": False, "error": "nope"}
    resp = cad_operation(
        fake_freecad, True, "edit_object", "Doc", obj_name="Box", obj_properties={}
    )
    assert "nope" in _text(resp)
    assert not _has_image(resp)


def test_delete_object_with_screenshot_single_rpc(fake_freecad):
    resp = cad_operation(
        fake_freecad, True, "delete_object", "Doc", obj_name="Box", auto_audit=False
    )
    assert fake_freecad.called_methods() == ["delete_object"]
    assert _has_image(resp)


# --- execute_code / async ---------------------------------------------------


def test_execute_code_with_screenshot_uses_inline(fake_freecad):
    """execute_code now uses inline screenshot (single RPC), not a second call."""
    resp = execute_code_operation(fake_freecad, True, "print(1)")
    assert fake_freecad.called_methods() == ["execute_code"]
    _, _args, kwargs = fake_freecad.calls[0]
    # screenshot params should be passed to execute_code
    assert kwargs.get("screenshot") == {"view_name": "Isometric"}
    assert _has_image(resp)


def test_execute_code_without_screenshot_single_call(fake_freecad):
    resp = execute_code_operation(fake_freecad, False, "print(1)")
    assert fake_freecad.called_methods() == ["execute_code"]
    assert not _has_image(resp)


def test_execute_code_failure_skips_screenshot(fake_freecad):
    fake_freecad.result_overrides["execute_code"] = {"success": False, "error": "syntax"}
    resp = execute_code_operation(fake_freecad, True, "!!!")
    assert fake_freecad.called_methods() == ["execute_code"]
    assert "syntax" in _text(resp)


def test_execute_code_async_response_contains_task_id(fake_freecad):
    resp = execute_code_async_operation(fake_freecad, "heavy()")
    text = _text(resp)
    assert "abc12345" in text
    assert "get_task_result" in text


def test_execute_code_async_failure(fake_freecad):
    fake_freecad.result_overrides["execute_code_async"] = {"success": False, "error": "busy"}
    resp = execute_code_async_operation(fake_freecad, "heavy()")
    assert "busy" in _text(resp)


def test_get_task_result_returns_json(fake_freecad):
    resp = get_task_result_operation(fake_freecad, "abc12345")
    data = json.loads(resp[0].text)
    assert data["status"] == "done"
    assert data["output"] == "hello"


def test_get_task_result_unknown_id(fake_freecad):
    fake_freecad.result_overrides["get_task_result"] = {
        "success": False,
        "error": "unknown task_id: xyz",
    }
    resp = get_task_result_operation(fake_freecad, "xyz")
    assert "unknown task_id" in _text(resp)


# --- execute_operations -----------------------------------------------------


def test_execute_operations_passes_ops_and_single_screenshot(fake_freecad):
    ops = [
        {"action": "create_object", "obj_type": "Part::Box", "obj_name": "Box"},
        {"action": "edit_object", "obj_name": "Box", "obj_properties": {"Length": 5}},
        {"action": "delete_object", "obj_name": "Box"},
    ]
    resp = execute_operations_operation(fake_freecad, True, "Doc", ops)
    assert fake_freecad.called_methods() == ["execute_operations"]
    _, args, _ = fake_freecad.calls[0]
    assert args[0] == "Doc"
    assert args[1] == ops
    assert args[3] == {"view_name": "Isometric"}  # screenshot params position
    assert _has_image(resp)


def test_execute_operations_without_screenshot(fake_freecad):
    resp = execute_operations_operation(
        fake_freecad, False, "Doc", [{"action": "delete_object", "obj_name": "Box"}]
    )
    _, args, _ = fake_freecad.calls[0]
    assert args[3] is None
    assert not _has_image(resp)


def test_execute_operations_reports_per_op_results(fake_freecad):
    fake_freecad.result_overrides["execute_operations"] = {
        "success": False,
        "results": [
            {"success": True, "action": "create_object", "object_name": "Box"},
            {"success": False, "action": "edit_object", "error": "bad prop"},
        ],
    }
    ops = [
        {"action": "create_object", "obj_type": "Part::Box", "obj_name": "Box"},
        {"action": "edit_object", "obj_name": "Box", "obj_properties": {}},
    ]
    resp = execute_operations_operation(fake_freecad, False, "Doc", ops)
    data = json.loads(resp[0].text)
    assert data["success"] is False
    assert data["results"][1]["error"] == "bad prop"


# --- misc read operations ----------------------------------------------------


def test_get_objects_returns_compact_json_and_screenshot(fake_freecad):
    fake_freecad.objects_by_doc["Doc"] = ["Box"]
    resp = get_objects_operation(fake_freecad, True, "Doc")
    assert fake_freecad.called_methods() == ["get_objects", "get_active_screenshot"]
    data = json.loads(resp[0].text)
    assert data[0]["Name"] == "Box"
    assert _has_image(resp)


def test_get_object_success(fake_freecad):
    resp = get_object_operation(fake_freecad, False, "Doc", "Box")
    data = json.loads(resp[0].text)
    assert data["TypeId"] == "Part::Box"


def test_list_documents(fake_freecad):
    fake_freecad.documents = ["Doc1"]
    resp = list_documents_operation(fake_freecad)
    data = json.loads(resp[0].text)
    assert data["success"] is True
    assert data["documents"] == ["Doc1"]


def test_create_document(fake_freecad):
    resp = create_document_operation(fake_freecad, "MyDoc")
    assert "MyDoc" in _text(resp)


def test_create_document_with_screenshot(fake_freecad):
    resp = create_document_operation(fake_freecad, "MyDoc", with_screenshot=True)
    assert "MyDoc" in _text(resp)
    assert fake_freecad.called_methods() == ["create_document"]
    _, _args, kwargs = fake_freecad.calls[0]
    assert kwargs.get("screenshot") == {"view_name": "Isometric"}


def test_get_view_returns_image(fake_freecad):
    resp = get_view_operation(fake_freecad, "Front")
    assert _has_image(resp)


# --- _normalize_object_names ------------------------------------------------


def test_normalize_object_names_from_strings():
    from cadpilot.operations.core import _normalize_object_names

    assert _normalize_object_names(["Cylinder", "Box", "Fuse"]) == ["Box", "Cylinder", "Fuse"]


def test_normalize_object_names_from_dicts():
    from cadpilot.operations.core import _normalize_object_names

    objects = [
        {"Name": "Cylinder", "TypeId": "Part::Cylinder"},
        {"Name": "Box", "TypeId": "Part::Box"},
    ]
    assert _normalize_object_names(objects) == ["Box", "Cylinder"]


def test_normalize_object_names_mixed():
    from cadpilot.operations.core import _normalize_object_names

    objects = ["Cylinder", {"Name": "Box", "TypeId": "Part::Box"}]
    assert _normalize_object_names(objects) == ["Box", "Cylinder"]


def test_normalize_object_names_empty():
    from cadpilot.operations.core import _normalize_object_names

    assert _normalize_object_names([]) == []
    assert _normalize_object_names(None) == []


# --- new positioning tools ---------------------------------------------------


def test_cad_move_translate(fake_freecad):
    resp = cad_operation(
        fake_freecad,
        False,
        "move",
        "Doc",
        obj_name="Box",
        obj_properties={"translate": {"x": 10, "y": 0, "z": 0}},
        auto_audit=False,
    )
    assert "move" in _text(resp).lower() or "success" in _text(resp).lower()
    # Should have called create_feature with type=move
    assert fake_freecad.called_methods() == ["create_feature"]


def test_cad_move_rotate(fake_freecad):
    resp = cad_operation(
        fake_freecad,
        False,
        "move",
        "Doc",
        obj_name="Box",
        obj_properties={"rotate": {"axis": {"x": 0, "y": 0, "z": 1}, "angle": 45}},
    )
    assert "move" in _text(resp).lower() or "success" in _text(resp).lower()


def test_get_positioning_info_face(fake_freecad):
    resp = get_positioning_info_operation(fake_freecad, "Doc", "Box", "face", 0)
    data = json.loads(resp[0].text)
    assert data["success"] is True
    assert data["element"] == "face"
    assert "center" in data
    assert "normal" in data


def test_get_positioning_info_edge(fake_freecad):
    resp = get_positioning_info_operation(fake_freecad, "Doc", "Box", "edge", 0)
    data = json.loads(resp[0].text)
    assert data["success"] is True
    assert data["element"] == "edge"


def test_align_shapes_touch(fake_freecad):
    resp = align_shapes_operation(
        fake_freecad,
        "Doc",
        "Box1",
        "face",
        0,
        "Box2",
        "face",
        0,
        mode="touch",
    )
    data = json.loads(resp[0].text)
    assert data["success"] is True
    assert "new_placement" in data
    assert fake_freecad.called_methods() == ["align_shapes"]


def test_align_shapes_center(fake_freecad):
    resp = align_shapes_operation(
        fake_freecad,
        "Doc",
        "Box1",
        "edge",
        0,
        "Box2",
        "edge",
        0,
        mode="center",
    )
    data = json.loads(resp[0].text)
    assert data["success"] is True


def test_get_objects_document_not_found_returns_error_text(fake_freecad):
    # With the fixed addon, a missing document surfaces as an error (raised by
    # the client), not as a misleading empty list.
    fake_freecad.errors["get_objects"] = RuntimeError("Document 'Nope' not found.")
    resp = get_objects_operation(fake_freecad, False, "Nope")
    assert "not found" in resp[0].text
