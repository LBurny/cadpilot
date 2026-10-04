"""Tests for cadpilot.operations.core using the fake connection."""

import json

import pytest
from mcp.types import TextContent

from cadpilot.operations import (
    align_shapes_operation,
    cad_operation,
    create_document_operation,
    execute_code_async_operation,
    execute_code_operation,
    execute_operations_operation,
    get_objects_operation,
    get_positioning_info_operation,
    get_task_result_operation,
    get_view_operation,
    list_documents_operation,
)


@pytest.fixture(autouse=True)
def _shot_dir(tmp_path, monkeypatch):
    """get_view writes screenshots to disk; keep them out of the real home dir."""
    monkeypatch.setenv("CADPILOT_HOME", str(tmp_path))


def _text(resp) -> str:
    return " ".join(c.text for c in resp if isinstance(c, TextContent))


def _has_shot(resp) -> bool:
    return "Screenshot saved to" in _text(resp)


# --- create_object (via cad) ------------------------------------------------


def test_create_object_single_rpc(fake_freecad):
    resp = cad_operation(
        fake_freecad,
        "create_object",
        "Doc",
        obj_type="Part::Box",
        obj_name="Box",
        auto_audit=False,
    )
    assert fake_freecad.called_methods() == ["create_object"]
    _, args, _ = fake_freecad.calls[0]
    assert args[1] == {"Name": "Box", "Type": "Part::Box", "Properties": {}}
    assert "created successfully" in _text(resp)


def test_create_object_failure_reports_error(fake_freecad):
    fake_freecad.result_overrides["create_object"] = {"success": False, "error": "boom"}
    resp = cad_operation(fake_freecad, "create_object", "Doc", obj_type="Part::Box", obj_name="Box")
    assert "boom" in _text(resp)


def test_create_object_exception_is_caught(fake_freecad):
    fake_freecad.errors["create_object"] = ConnectionResetError("reset")
    resp = cad_operation(fake_freecad, "create_object", "Doc", obj_type="Part::Box", obj_name="Box")
    assert "cad create_object failed" in _text(resp)


# --- edit / delete (via cad) ------------------------------------------------


def test_edit_object_single_rpc(fake_freecad):
    resp = cad_operation(
        fake_freecad,
        "edit_object",
        "Doc",
        obj_name="Box",
        obj_properties={"Length": 5},
        auto_audit=False,
    )
    assert fake_freecad.called_methods() == ["edit_object"]
    _, args, _ = fake_freecad.calls[0]
    assert args[2] == {"Properties": {"Length": 5}}
    assert "edited successfully" in _text(resp)


def test_edit_object_failure_reports_error(fake_freecad):
    fake_freecad.result_overrides["edit_object"] = {"success": False, "error": "nope"}
    resp = cad_operation(fake_freecad, "edit_object", "Doc", obj_name="Box", obj_properties={})
    assert "nope" in _text(resp)


def test_delete_object_single_rpc(fake_freecad):
    resp = cad_operation(
        fake_freecad,
        "delete_object",
        "Doc",
        obj_name="Box",
        auto_audit=False,
    )
    assert fake_freecad.called_methods() == ["delete_object"]
    assert "deleted successfully" in _text(resp)


# --- execute_code / async ---------------------------------------------------


def test_execute_code_single_call(fake_freecad):
    resp = execute_code_operation(fake_freecad, "print(1)")
    assert fake_freecad.called_methods() == ["execute_code"]
    assert "executed successfully" in _text(resp)


def test_execute_code_failure_reports_error(fake_freecad):
    fake_freecad.result_overrides["execute_code"] = {"success": False, "error": "syntax"}
    resp = execute_code_operation(fake_freecad, "!!!")
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


def test_execute_operations_passes_ops(fake_freecad):
    ops = [
        {"action": "create_object", "obj_type": "Part::Box", "obj_name": "Box"},
        {"action": "edit_object", "obj_name": "Box", "obj_properties": {"Length": 5}},
        {"action": "delete_object", "obj_name": "Box"},
    ]
    resp = execute_operations_operation(fake_freecad, "Doc", ops)
    assert fake_freecad.called_methods() == ["execute_operations"]
    _, args, _ = fake_freecad.calls[0]
    assert args[0] == "Doc"
    assert args[1] == ops
    assert "3/3" in _text(resp)


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
    resp = execute_operations_operation(fake_freecad, "Doc", ops)
    data = json.loads(resp[0].text)
    assert data["success"] is False
    assert data["results"][1]["error"] == "bad prop"


# --- misc read operations ----------------------------------------------------


def test_get_objects_returns_compact_json(fake_freecad):
    fake_freecad.objects_by_doc["Doc"] = ["Box"]
    resp = get_objects_operation(fake_freecad, "Doc")
    assert fake_freecad.called_methods() == ["get_objects"]
    data = json.loads(resp[0].text)
    assert data[0]["Name"] == "Box"


def test_get_objects_with_obj_name_returns_one_object(fake_freecad):
    resp = get_objects_operation(fake_freecad, "Doc", "Box")
    assert fake_freecad.called_methods() == ["get_object"]
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
    assert fake_freecad.called_methods() == ["create_document"]


def test_get_view_returns_path(fake_freecad):
    resp = get_view_operation(fake_freecad, "Front")
    assert _has_shot(resp)


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
    resp = get_objects_operation(fake_freecad, "Nope")
    assert "not found" in resp[0].text


# --- session action dispatch -------------------------------------------------


def test_session_action_requires_doc_name_for_start(fake_freecad):
    from cadpilot.operations import session_action_operation

    resp = session_action_operation(fake_freecad, "start")
    assert "requires doc_name" in _text(resp)


def test_session_action_requires_to_step_for_rollback(fake_freecad):
    """A bare rollback must fail loudly: to_step=0 would undo ALL steps."""
    from cadpilot.operations import session_action_operation

    resp = session_action_operation(fake_freecad, "rollback")
    assert "requires to_step" in _text(resp)


def test_session_action_unknown(fake_freecad):
    from cadpilot.operations import session_action_operation

    resp = session_action_operation(fake_freecad, "fly_to_moon")
    assert "unknown session action" in _text(resp)


# --- multi-agent document binding -------------------------------------------


def test_execute_code_passes_explicit_doc_name(fake_freecad):
    """doc_name binds transaction + journal step + ActiveDocument to ONE
    document; under two agents the active doc is the other agent's."""
    execute_code_operation(fake_freecad, "print(1)", doc_name="OtherDoc")
    ((_m, _a, kw),) = [c for c in fake_freecad.calls if c[0] == "execute_code"]
    assert kw.get("doc_name") == "OtherDoc"


def test_execute_code_binds_active_session_document_automatically(fake_freecad):
    """The session flow must not have to remember doc_name: an active session
    binds its document. An explicit doc_name wins over the session, and a
    process that has touched nothing keeps the legacy None (active-document)
    behavior."""
    from cadpilot.operations.core import reset_last_doc_name
    from cadpilot.session_state import new_session, set_current_session

    sess = new_session("bind test", "SessDoc")
    set_current_session(sess)
    try:
        execute_code_operation(fake_freecad, "print(1)")
        (_m, _a, kw) = [c for c in fake_freecad.calls if c[0] == "execute_code"][-1]
        assert kw.get("doc_name") == "SessDoc"
        execute_code_operation(fake_freecad, "print(1)", doc_name="Explicit")
        (_m, _a, kw) = [c for c in fake_freecad.calls if c[0] == "execute_code"][-1]
        assert kw.get("doc_name") == "Explicit"
        set_current_session(None)
        reset_last_doc_name()  # the explicit call above became the home document
        execute_code_operation(fake_freecad, "print(1)")
        (_m, _a, kw) = [c for c in fake_freecad.calls if c[0] == "execute_code"][-1]
        assert kw.get("doc_name") is None
    finally:
        set_current_session(None)


def test_execute_code_falls_back_to_home_document(fake_freecad):
    """The home document closes the mixing hole for agents that pass neither
    doc_name nor a session: create_document marks it, and a later unbound
    execute_code binds there instead of the global active document."""
    create_document_operation(fake_freecad, "DocA")
    execute_code_operation(fake_freecad, "print(1)")
    (_m, _a, kw) = [c for c in fake_freecad.calls if c[0] == "execute_code"][-1]
    assert kw.get("doc_name") == "DocA"


def test_execute_code_home_document_follows_last_named_mutation(fake_freecad):
    """cad() names its document explicitly, so it moves the home document;
    an explicit execute_code doc_name still wins over it."""
    create_document_operation(fake_freecad, "DocA")
    cad_operation(
        fake_freecad,
        "create_object",
        "DocB",
        obj_type="Part::Box",
        obj_name="Box",
    )
    execute_code_operation(fake_freecad, "print(1)")
    (_m, _a, kw) = [c for c in fake_freecad.calls if c[0] == "execute_code"][-1]
    assert kw.get("doc_name") == "DocB"
    execute_code_operation(fake_freecad, "print(1)", doc_name="DocC")
    (_m, _a, kw) = [c for c in fake_freecad.calls if c[0] == "execute_code"][-1]
    assert kw.get("doc_name") == "DocC"


def test_execute_code_home_binding_is_disclosed_in_the_reply(fake_freecad):
    """A home-bound run must say which document absorbed it, so the model can
    catch a wrong binding from the reply alone."""
    create_document_operation(fake_freecad, "DocA")
    resp = execute_code_operation(fake_freecad, "print(1)")
    assert "DocA" in resp[0].text
    assert "doc_name" in resp[0].text
