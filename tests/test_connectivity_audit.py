"""Auto connectivity audit after cad() mutations."""

from mcp.types import TextContent

from cadpilot.operations import cad_operation
from cadpilot.operations.core import _format_connectivity_warning


def _text(resp) -> str:
    return " ".join(c.text for c in resp if isinstance(c, TextContent))


def _audit_with_islands():
    return {
        "success": True,
        "object_count": 5,
        "floating": [],
        "interferences": [],
        "checks": [],
        "islands": [
            {"objects": ["Spoke_0", "Spoke_1"], "size": 2, "gap_mm": 1.998, "nearest_main": "Rim"}
        ],
        "summary": {
            "floating_count": 0,
            "interference_count": 0,
            "checks_failed": 0,
            "island_count": 1,
            "component_count": 2,
        },
    }


def test_committed_mutation_appends_island_warning(fake_freecad):
    fake_freecad.result_overrides["verify_assembly"] = _audit_with_islands()
    resp = cad_operation(
        fake_freecad, False, "create_object", "Doc", obj_type="Part::Box", obj_name="Box"
    )
    assert "island" in _text(resp).lower()
    assert "Spoke_0" in _text(resp)
    assert fake_freecad.called_methods() == ["create_object", "verify_assembly"]


def test_no_islands_no_warning(fake_freecad):
    resp = cad_operation(
        fake_freecad, False, "create_object", "Doc", obj_type="Part::Box", obj_name="Box"
    )
    assert "island" not in _text(resp).lower()


def test_old_addon_without_islands_key_is_silent(fake_freecad):
    fake_freecad.result_overrides["verify_assembly"] = {"success": True, "object_count": 3}
    resp = cad_operation(
        fake_freecad, False, "create_object", "Doc", obj_type="Part::Box", obj_name="Box"
    )
    assert "created successfully" in _text(resp)
    assert "island" not in _text(resp).lower()


def test_auto_audit_disabled_skips_rpc(fake_freecad):
    resp = cad_operation(
        fake_freecad,
        False,
        "create_object",
        "Doc",
        obj_type="Part::Box",
        obj_name="Box",
        auto_audit=False,
    )
    assert "verify_assembly" not in fake_freecad.called_methods()
    assert "created successfully" in _text(resp)


def test_failed_mutation_skips_audit(fake_freecad):
    fake_freecad.result_overrides["create_object"] = {"success": False, "error": "boom"}
    cad_operation(fake_freecad, False, "create_object", "Doc", obj_type="Part::Box", obj_name="Box")
    assert "verify_assembly" not in fake_freecad.called_methods()


def test_audit_error_does_not_break_mutation(fake_freecad):
    fake_freecad.errors["verify_assembly"] = RuntimeError("rpc down")
    resp = cad_operation(
        fake_freecad, False, "create_object", "Doc", obj_type="Part::Box", obj_name="Box"
    )
    assert "created successfully" in _text(resp)


def test_large_document_skips_audit(fake_freecad):
    fake_freecad.result_overrides["verify_assembly"] = {
        "success": True,
        "object_count": 500,
        "islands": [{"objects": ["A"], "size": 1, "gap_mm": 3.0, "nearest_main": "B"}],
        "summary": {"island_count": 1},
    }
    resp = cad_operation(
        fake_freecad, False, "create_object", "Doc", obj_type="Part::Box", obj_name="Box"
    )
    assert "island" not in _text(resp).lower()


def test_format_warning_truncates_object_lists():
    audit = {
        "islands": [
            {
                "objects": [f"Obj_{i}" for i in range(8)],
                "size": 8,
                "gap_mm": 2.0,
                "nearest_main": "Main",
            }
        ],
        "summary": {"island_count": 1},
    }
    text = _format_connectivity_warning(audit)
    assert "Obj_0" in text and "+4 more" in text


def test_format_warning_empty_when_no_islands():
    assert _format_connectivity_warning({"islands": [], "summary": {"island_count": 0}}) == ""
