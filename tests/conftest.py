"""Shared fixtures: an in-memory stand-in for FreeCADConnection.

The fake records every call (method name, args, kwargs) so tests can assert
on RPC call patterns (e.g. that the screenshot is requested inline instead of
via a second get_active_screenshot call).
"""

import pytest

from cadpilot.session_state import set_current_session


class FakeFreeCADConnection:
    SCREENSHOT = "ZmFrZS1wbmc="  # base64 placeholder payload

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        # method name -> exception to raise on next call
        self.errors: dict[str, Exception] = {}
        # method name -> dict returned instead of the default success result
        self.result_overrides: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        # document name -> sorted object names (state fingerprint source)
        self.objects_by_doc: dict[str, list[str]] = {}
        # documents that exist (for list_documents)
        self.documents: list[str] = []
        # undo_transactions returns this many undone (None = use requested n)
        self.undo_count: int | None = None

    def _record(self, method: str, *args, **kwargs):
        self.calls.append((method, args, kwargs))
        if method in self.errors:
            raise self.errors.pop(method)

    def _result(self, method: str, default: dict) -> dict:
        return self.result_overrides.get(method, default)

    def _doc_objects(self, doc_name: str) -> list[str]:
        return sorted(self.objects_by_doc.get(doc_name, []))

    # --- RPC methods mirrored from FreeCADConnection ---------------------

    def ping(self) -> bool:
        self._record("ping")
        return True

    def create_document(self, name, screenshot=None):
        self._record("create_document", name, screenshot=screenshot)
        self.documents.append(name)
        res = self._result("create_document", {"success": True, "document_name": name})
        if screenshot is not None:
            res["screenshot"] = self.SCREENSHOT
        return res

    def create_object(self, doc_name, obj_data, screenshot=None):
        self._record("create_object", doc_name, obj_data, screenshot)
        res = {
            "success": True,
            "object_name": obj_data.get("Name", "New_Object"),
            "transaction": True,
            "objects": self._doc_objects(doc_name),
        }
        if screenshot is not None:
            res["screenshot"] = self.SCREENSHOT
        return self._result("create_object", res)

    def edit_object(self, doc_name, obj_name, obj_data, screenshot=None):
        self._record("edit_object", doc_name, obj_name, obj_data, screenshot)
        res = {
            "success": True,
            "object_name": obj_name,
            "transaction": True,
            "objects": self._doc_objects(doc_name),
        }
        if screenshot is not None:
            res["screenshot"] = self.SCREENSHOT
        return self._result("edit_object", res)

    def delete_object(self, doc_name, obj_name, screenshot=None):
        self._record("delete_object", doc_name, obj_name, screenshot)
        res = {
            "success": True,
            "object_name": obj_name,
            "transaction": True,
            "objects": self._doc_objects(doc_name),
        }
        if screenshot is not None:
            res["screenshot"] = self.SCREENSHOT
        return self._result("delete_object", res)

    def create_feature(self, doc_name, spec, screenshot=None):
        self._record("create_feature", doc_name, spec, screenshot)
        res = {
            "success": True,
            "object_name": spec.get("name") or spec["type"].capitalize(),
            "transaction": True,
            "objects": self._doc_objects(doc_name),
        }
        if screenshot is not None:
            res["screenshot"] = self.SCREENSHOT
        return self._result("create_feature", res)

    def execute_code(self, code, screenshot=None):
        self._record("execute_code", code, screenshot=screenshot)
        res = self._result(
            "execute_code", {"success": True, "message": "Python code executed successfully."}
        )
        if screenshot is not None:
            res["screenshot"] = self.SCREENSHOT
        return res

    def execute_code_async(self, code):
        self._record("execute_code_async", code)
        return self._result(
            "execute_code_async",
            {
                "success": True,
                "task_id": "abc12345",
                "message": "Code execution started in background.",
            },
        )

    def get_task_result(self, task_id):
        self._record("get_task_result", task_id)
        return self._result(
            "get_task_result",
            {
                "success": True,
                "task_id": task_id,
                "status": "done",
                "output": "hello",
                "error": None,
            },
        )

    def get_active_screenshot(
        self, view_name="Isometric", width=None, height=None, focus_object=None
    ):
        self._record("get_active_screenshot", view_name, width, height, focus_object)
        return self.SCREENSHOT

    def get_objects(self, doc_name):
        self._record("get_objects", doc_name)
        return [{"Name": n, "TypeId": "Part::Box"} for n in self._doc_objects(doc_name)]

    def get_object(self, doc_name, obj_name):
        self._record("get_object", doc_name, obj_name)
        return {"Name": obj_name, "TypeId": "Part::Box"}

    def list_documents(self):
        self._record("list_documents")
        return list(self.documents)

    def execute_operations(self, doc_name, ops, stop_on_error=False, screenshot=None):
        self._record("execute_operations", doc_name, ops, stop_on_error, screenshot)
        res = {
            "success": True,
            "results": [{"success": True, "action": op.get("action")} for op in ops],
            "transaction": True,
            "objects": self._doc_objects(doc_name),
        }
        if screenshot is not None:
            res["screenshot"] = self.SCREENSHOT
        return self._result("execute_operations", res)

    def undo_transactions(self, doc_name, n=1):
        self._record("undo_transactions", doc_name, n)
        count = self.undo_count if self.undo_count is not None else n
        return self._result(
            "undo_transactions",
            {"success": True, "count": count, "objects": self._doc_objects(doc_name)},
        )

    def redo_transactions(self, doc_name, n=1):
        self._record("redo_transactions", doc_name, n)
        return self._result(
            "redo_transactions",
            {"success": True, "count": n, "objects": self._doc_objects(doc_name)},
        )

    def save_document(self, doc_name, path=None):
        self._record("save_document", doc_name, path)
        return self._result(
            "save_document", {"success": True, "file_name": path or f"{doc_name}.FCStd"}
        )

    def inspect_freecad(self, doc_name=None, obj_name=None, dotted_name=None):
        self._record("inspect_freecad", doc_name, obj_name, dotted_name)
        if obj_name:
            return self._result(
                "inspect_freecad",
                {
                    "success": True,
                    "kind": "object",
                    "name": obj_name,
                    "type_id": "Part::Box",
                    "properties": {"Length": "App::PropertyLength"},
                    "methods": ["extrude"],
                    "doc": "Box object",
                },
            )
        return self._result(
            "inspect_freecad",
            {"success": True, "kind": "api", "name": dotted_name, "doc": "makeLoft(...) docs"},
        )

    def measure_geometry(self, doc_name, obj_name):
        self._record("measure_geometry", doc_name, obj_name)
        return self._result(
            "measure_geometry",
            {
                "success": True,
                "shape_type": "Solid",
                "is_valid": True,
                "volume_mm3": 1000.0,
                "area_mm2": 600.0,
                "center_of_mass": [5.0, 5.0, 5.0],
                "bbox": {"xmin": 0, "ymin": 0, "zmin": 0, "xmax": 10, "ymax": 10, "zmax": 10},
                "counts": {"solids": 1, "shells": 1, "faces": 6, "edges": 12, "vertices": 8},
            },
        )

    def get_topology(self, doc_name, obj_name, element="faces", limit=50, offset=0):
        self._record("get_topology", doc_name, obj_name, element, limit, offset)
        return self._result(
            "get_topology",
            {
                "success": True,
                "element": element,
                "total": 6,
                "returned": 1,
                "offset": offset,
                element: [
                    {
                        "index": 0,
                        "name": "Face1",
                        "type": "Plane",
                        "area": 100.0,
                        "center": [5.0, 5.0, 0.0],
                        "normal": [0, 0, 1],
                    }
                ],
            },
        )

    def check_interference(self, doc_name, obj_a, obj_b):
        self._record("check_interference", doc_name, obj_a, obj_b)
        return self._result(
            "check_interference",
            {
                "success": True,
                "distance_mm": 0.0,
                "intersects": True,
                "common_volume_mm3": 12.5,
            },
        )

    def get_positioning_info(self, doc_name, obj_name, element, element_index):
        self._record("get_positioning_info", doc_name, obj_name, element, element_index)
        return self._result(
            "get_positioning_info",
            {
                "success": True,
                "element": element,
                "name": f"{element.capitalize()}{element_index + 1}",
                "type": "Plane",
                "area": 100.0,
                "center": [5.0, 5.0, 0.0],
                "normal": [0, 0, 1],
                "placement": {"base": [0, 0, 0], "rotation": {"axis": [0, 0, 1], "angle_deg": 0}},
            },
        )

    def align_shapes(
        self,
        doc_name,
        obj_name,
        element,
        element_index,
        target_obj_name,
        target_element,
        target_element_index,
        mode="touch",
        offset=0.0,
    ):
        self._record(
            "align_shapes",
            doc_name,
            obj_name,
            element,
            element_index,
            target_obj_name,
            target_element,
            target_element_index,
            mode,
            offset,
        )
        return self._result(
            "align_shapes",
            {
                "success": True,
                "obj_name": obj_name,
                "new_placement": {
                    "base": [0, 0, 0],
                    "rotation": {"axis": [0, 0, 1], "angle_deg": 0},
                },
                "transaction": True,
                "objects": self._doc_objects(doc_name),
            },
        )

    def get_anchors(self, doc_name, obj_name):
        self._record("get_anchors", doc_name, obj_name)
        return self._result(
            "get_anchors",
            {
                "success": True,
                "obj_name": obj_name,
                "anchors": {
                    "bbox_center": {"pos": [5, 5, 5], "dir": None, "source": "auto"},
                    "axle": {"pos": [0, 0, 70], "dir": [0, 1, 0], "source": "explicit"},
                },
            },
        )

    def set_anchors(
        self, doc_name, obj_name, anchors, replace=False, coord_frame="local", screenshot=None
    ):
        self._record("set_anchors", doc_name, obj_name, anchors, replace, coord_frame, screenshot)
        res = {
            "success": True,
            "obj_name": obj_name,
            "anchor_count": len(anchors),
            "transaction": True,
            "objects": self._doc_objects(doc_name),
        }
        if screenshot is not None:
            res["screenshot"] = self.SCREENSHOT
        return self._result("set_anchors", res)

    def assemble(self, doc_name, mates, tolerance=0.1, stop_on_error=True, screenshot=None):
        self._record("assemble", doc_name, mates, tolerance, stop_on_error, screenshot)
        res = {
            "success": True,
            "passed": len(mates),
            "failed": 0,
            "mates": [
                {"obj": m["obj"], "anchor": m["anchor"], "passed": True, "residual_mm": 0.0}
                for m in mates
            ],
            "transaction": True,
            "objects": self._doc_objects(doc_name),
        }
        if screenshot is not None:
            res["screenshot"] = self.SCREENSHOT
        return self._result("assemble", res)

    def verify_assembly(
        self, doc_name, checks=None, float_threshold=1.0, interference_min_volume=1.0
    ):
        self._record("verify_assembly", doc_name, checks, float_threshold, interference_min_volume)
        return self._result(
            "verify_assembly",
            {
                "success": True,
                "object_count": 2,
                "floating": [],
                "interferences": [],
                "checks": [
                    {
                        "obj": "A",
                        "anchor": "p",
                        "target": "B",
                        "target_anchor": "q",
                        "distance_mm": 0.0,
                        "tolerance": 0.1,
                        "passed": True,
                    }
                ]
                if checks
                else [],
                "summary": {"floating_count": 0, "interference_count": 0, "checks_failed": 0},
            },
        )

    # --- helpers for assertions ------------------------------------------

    def called_methods(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def fake_freecad() -> FakeFreeCADConnection:
    return FakeFreeCADConnection()


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point session/pattern persistence at a temp dir and reset the
    global current-session registry around the test."""
    monkeypatch.setenv("CADPILOT_HOME", str(tmp_path))
    set_current_session(None)
    yield tmp_path
    set_current_session(None)
