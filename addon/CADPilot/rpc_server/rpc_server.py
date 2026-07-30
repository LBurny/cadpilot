import base64
import contextlib
import io
import os
import tempfile
import threading
import traceback
import uuid
from typing import Any

import FreeCAD
import FreeCADGui
from PySide import QtCore

from rpc_server.assembly_ops import (
    assemble as _assemble,
)
from rpc_server.assembly_ops import (
    get_anchors as _get_anchors,
)
from rpc_server.assembly_ops import (
    set_anchors as _set_anchors,
)
from rpc_server.assembly_ops import (
    verify_assembly as _verify_assembly,
)
from rpc_server.commands import register_commands, schedule_toggle_sync
from rpc_server.feature_ops import FEATURE_TYPES, create_feature_gui, describe_feature
from rpc_server.geometry_query import (
    align_shapes as _align_shapes,
)
from rpc_server.geometry_query import (
    check_interference as _check_interference,
)
from rpc_server.geometry_query import (
    get_positioning_info as _get_positioning_info,
)
from rpc_server.geometry_query import (
    get_topology as _get_topology,
)
from rpc_server.geometry_query import (
    measure_geometry as _measure_geometry,
)
from rpc_server.gui_dispatch import (
    cleanup_waker,
    dispatch_to_gui,
    init_waker,
    process_gui_tasks,
    request_shutdown,
)
from rpc_server.ip_filter import FilteredXMLRPCServer
from rpc_server.joint_ops import assembly_op as _assembly_op
from rpc_server.object_factory import create_object_gui
from rpc_server.property_mapper import Object, set_object_property
from rpc_server.serialize import serialize_object
from rpc_server.settings import load_settings
from rpc_server.view_manager import save_active_screenshot

rpc_server_thread = None
rpc_server_instance = None
_stop_thread = None  # drains shutdown off the GUI thread; see stop_rpc_server

# Background-task registry for execute_code_async / get_task_result.
# Insertion-ordered dict doubles as the FIFO eviction order.
_async_tasks: dict[str, dict] = {}
_async_tasks_lock = threading.Lock()
_ASYNC_TASKS_MAX = 50


def _ok(res) -> bool:
    """True when a GUI-thread handler returned success."""
    return res is True


def _err(res) -> dict:
    """Convert any non-True result (error string or timeout dict) to a failure dict."""
    if isinstance(res, dict):
        return res
    return {"success": False, "error": str(res)}


def _make_tmp_png() -> str:
    fd, tmp_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    return tmp_path


def _read_b64(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except OSError:
        return None


class FreeCADRPC:
    """RPC server for FreeCAD"""

    TIMEOUT = 60  # generous wait for GUI thread to become free
    EXECUTE_CODE_TIMEOUT = 90  # GUI-thread execution; use execute_code_async for heavy OCCT ops

    def ping(self):
        return True

    # --- mutations (with optional inline screenshot) ----------------------

    def _run_op_with_screenshot(
        self,
        gui_fn,
        success_payload: dict,
        screenshot: dict | None,
        doc_name: str | None = None,
        transaction: str | None = None,
        commit_if=None,
    ) -> dict:
        """Run ``gui_fn`` on the GUI thread and, when ``screenshot`` params are
        given and the op succeeded, capture the screenshot in the SAME GUI
        dispatch — a single RPC round trip with no race against intervening ops.

        ``gui_fn`` returns True / an error string / a result dict (which may
        itself carry ``success`` — batch ops use this to report partial
        failure with structured per-op results).
        ``success_payload`` provides the base fields of the success dict.

        When ``transaction`` is given, the op is wrapped in a FreeCAD
        document transaction (``doc.openTransaction(transaction)``) so
        modeling sessions can roll it back via ``undo_transactions``.
        ``commit_if(res)`` decides commit vs abort (default: commit when the
        op succeeded). If the transaction cannot be opened, the op still runs
        and the result carries ``"transaction": False``.
        Successful results include ``objects`` — the sorted document object
        names — as a cheap state fingerprint for rollback verification.
        """
        tmp_path = _make_tmp_png() if screenshot is not None else None

        def task():
            doc = None
            in_transaction = False
            if transaction:
                try:
                    doc = FreeCAD.getDocument(doc_name) if doc_name else FreeCAD.ActiveDocument
                except Exception:
                    doc = None
                if doc is not None:
                    try:
                        doc.openTransaction(transaction)
                        in_transaction = True
                    except Exception as e:
                        FreeCAD.Console.PrintWarning(f"CADPilot: cannot open transaction: {e}\n")
            try:
                res = gui_fn()
            except Exception:
                if in_transaction:
                    doc.abortTransaction()
                raise
            ok = res is True or (isinstance(res, dict) and res.get("success"))
            should_commit = ok if commit_if is None else bool(commit_if(res))
            if in_transaction:
                if should_commit:
                    doc.commitTransaction()
                else:
                    doc.abortTransaction()
            # Whenever a transaction was committed the document changed, so the
            # caller needs the fingerprint even for partial batch failures —
            # otherwise the session log and the undo stack would desync.
            if not should_commit:
                return res, None, in_transaction
            # Resolve the document for the fingerprint even when the op itself
            # created it (defensive: no caller passes doc_name=None today).
            if doc is None:
                doc = FreeCAD.ActiveDocument
            objects = []
            if doc is not None:
                try:
                    objects = sorted(o.Name for o in doc.Objects)
                except Exception:
                    objects = []
            if tmp_path is None:
                return res, None, in_transaction, objects
            shot = save_active_screenshot(
                tmp_path,
                screenshot.get("view_name", "Isometric"),
                screenshot.get("width"),
                screenshot.get("height"),
                screenshot.get("focus_object"),
            )
            return res, tmp_path if shot is True else None, in_transaction, objects

        try:
            out = dispatch_to_gui(task)
            if not (isinstance(out, tuple) and len(out) >= 2):
                return _err(out)  # timeout dict or error string from the dispatch layer
            res, shot_path = out[0], out[1]
            in_transaction = out[2] if len(out) > 2 else False
            objects = out[3] if len(out) > 3 else None
            if isinstance(res, dict):
                result = {**success_payload, **res}
            elif res is True:
                result = dict(success_payload)
            else:
                return _err(res)
            if transaction:
                result["transaction"] = in_transaction
            if objects is not None:
                result["objects"] = objects
            if shot_path is not None:
                b64 = _read_b64(shot_path)
                if b64:
                    result["screenshot"] = b64
            return result
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    def create_document(self, name="New_Document", screenshot: dict | None = None):
        # The GUI handler reports the document's ACTUAL name — FreeCAD
        # sanitises requested names ("My Doc" -> "My_Doc") and de-duplicates
        # ("Doc" -> "Doc001"); reporting the requested name breaks every
        # follow-up call that uses it.
        return self._run_op_with_screenshot(
            lambda: self._create_document_gui(name),
            {"success": True},
            screenshot,
            doc_name=None,
            transaction=None,  # create_document is not undo-able in FreeCAD
        )

    def create_object(self, doc_name, obj_data: dict[str, Any], screenshot: dict | None = None):
        obj = Object(
            name=obj_data.get("Name", "New_Object"),
            type=obj_data["Type"],
            properties=obj_data.get("Properties", {}),
        )
        # create_object_gui reports the created object's actual Name (see
        # its docstring) — same sanitise/de-duplicate concern as documents.
        return self._run_op_with_screenshot(
            lambda: self._create_object_gui(doc_name, obj),
            {"success": True},
            screenshot,
            doc_name=doc_name,
            transaction=f"CADPilot: create_object {obj.name}",
        )

    def create_feature(self, doc_name, feature_spec: dict, screenshot: dict | None = None):
        def task():
            try:
                doc = FreeCAD.getDocument(doc_name)
            except Exception:
                return f"Document '{doc_name}' not found."
            try:
                feat = create_feature_gui(doc, feature_spec)
                FreeCAD.Console.PrintMessage(
                    f"Feature '{feat.Name}' ({feature_spec.get('type')}) created in '{doc_name}' via RPC.\n"
                )
                extra = describe_feature(feat, feature_spec)
                return {"success": True, "object_name": feat.Name, **extra}
            except Exception as e:
                return str(e)

        return self._run_op_with_screenshot(
            task,
            {"success": True},
            screenshot,
            doc_name=doc_name,
            transaction=f"CADPilot: {feature_spec.get('type', 'feature')} {feature_spec.get('base', '')}",
        )

    def assembly_op(self, doc_name: str, spec: dict):
        """Persistent-joint assembly session op (joint_ops/trim_ops)."""

        def task():
            try:
                doc = FreeCAD.getDocument(doc_name)
            except Exception:
                return f"Document '{doc_name}' not found."
            try:
                res = _assembly_op(doc, spec)
                FreeCAD.Console.PrintMessage(
                    f"Assembly op '{spec.get('operation')}' done in '{doc_name}' via RPC.\n"
                )
                return {"success": True, **res}
            except Exception as e:
                return str(e)

        return self._run_op_with_screenshot(
            task,
            {"success": True},
            None,
            doc_name=doc_name,
            transaction=f"CADPilot: assembly {spec.get('operation', 'op')}",
        )

    def edit_object(
        self,
        doc_name: str,
        obj_name: str,
        properties: dict[str, Any],
        screenshot: dict | None = None,
    ) -> dict[str, Any]:
        obj = Object(
            name=obj_name,
            properties=properties.get("Properties", {}),
        )
        return self._run_op_with_screenshot(
            lambda: self._edit_object_gui(doc_name, obj),
            {"success": True, "object_name": obj.name},
            screenshot,
            doc_name=doc_name,
            transaction=f"CADPilot: edit_object {obj.name}",
        )

    def delete_object(self, doc_name: str, obj_name: str, screenshot: dict | None = None):
        return self._run_op_with_screenshot(
            lambda: self._delete_object_gui(doc_name, obj_name),
            {"success": True, "object_name": obj_name},
            screenshot,
            doc_name=doc_name,
            transaction=f"CADPilot: delete_object {obj_name}",
        )

    def execute_operations(
        self, doc_name: str, ops: list, stop_on_error: bool = False, screenshot: dict | None = None
    ) -> dict[str, Any]:
        """Run a batch of create/edit/delete ops in ONE GUI dispatch.

        Each op: {"action": "create_object"|"edit_object"|"delete_object", ...}.
        Partial failure is reported per-op in "results"; the top-level
        "success" is True only when every executed op succeeded.
        """
        if not isinstance(ops, list) or not ops:
            return {"success": False, "error": "ops must be a non-empty list"}

        def _run_batch():
            results = []
            for op in ops:
                results.append(self._run_one_operation(doc_name, op, is_batch=True))
                if stop_on_error and not results[-1]["success"]:
                    break
            # Single recompute after all ops instead of per-object
            try:
                doc = FreeCAD.getDocument(doc_name)
                if doc:
                    doc.recompute()
            except Exception:
                pass
            return {"success": all(r["success"] for r in results), "results": results}

        # One undo unit per batch. Commit when at least one op succeeded —
        # aborting on partial failure would silently undo the ops the result
        # reports as successful.
        return self._run_op_with_screenshot(
            _run_batch,
            {"success": True},
            screenshot,
            doc_name=doc_name,
            transaction=f"CADPilot: batch ({len(ops)} ops)",
            commit_if=lambda res: any(r.get("success") for r in res.get("results", [])),
        )

    def _run_one_operation(self, doc_name: str, op, is_batch: bool = False) -> dict:
        """Run a single batch op on the GUI thread; returns a per-op result dict.

        When ``is_batch`` is True, create_object skips per-object recompute
        (the batch handler does one recompute after all ops).
        """
        action = op.get("action") if isinstance(op, dict) else None
        try:
            if action == "create_object":
                obj = Object(
                    name=op.get("obj_name", "New_Object"),
                    type=op["obj_type"],
                    properties=op.get("obj_properties", {}),
                )
                res = self._create_object_gui(doc_name, obj, recompute=not is_batch)
            elif action == "edit_object":
                obj = Object(name=op["obj_name"], properties=op.get("obj_properties", {}))
                res = self._edit_object_gui(doc_name, obj)
            elif action == "delete_object":
                res = self._delete_object_gui(doc_name, op["obj_name"])
            elif action in FEATURE_TYPES:
                try:
                    doc = FreeCAD.getDocument(doc_name)
                except Exception:
                    return {
                        "success": False,
                        "action": action,
                        "error": f"Document '{doc_name}' not found.",
                    }
                try:
                    spec = {
                        "type": action,
                        "base": op.get("obj_name"),
                        **(op.get("obj_properties") or {}),
                    }
                    res = {"success": True, "object_name": create_feature_gui(doc, spec).Name}
                except Exception as e:
                    return {"success": False, "action": action, "error": str(e)}
            else:
                return {"success": False, "action": action, "error": f"unknown action: {action!r}"}
        except Exception as e:
            return {"success": False, "action": action, "error": f"{type(e).__name__}: {e}"}
        if isinstance(res, dict) and res.get("success"):
            return {"success": True, "action": action, **res}
        if res is True:
            return {"success": True, "action": action, "object_name": op.get("obj_name")}
        return {"success": False, "action": action, "error": str(res)}

    # --- undo/redo, save, introspection (modeling-session support) -----------

    def _undo_redo(self, doc_name: str, n: int, undo: bool) -> dict[str, Any]:
        try:
            n = int(n)
        except (TypeError, ValueError):
            return {"success": False, "error": f"invalid count: {n!r}"}
        if n < 0:
            return {"success": False, "error": "count must be >= 0"}

        def task():
            try:
                doc = FreeCAD.getDocument(doc_name)
            except Exception:
                return f"Document '{doc_name}' not found."
            stack_attr = "UndoNames" if undo else "RedoNames"
            try:
                stack_before = list(getattr(doc, stack_attr, []) or [])
                stack_known = True
            except Exception:
                stack_before = []
                stack_known = False
            # When the stack is known to be empty there is nothing to undo —
            # don't attempt n blind undo() calls that only fail via exceptions.
            limit = min(n, len(stack_before)) if stack_known else n
            done = 0
            for _ in range(limit):
                try:
                    if undo:
                        doc.undo()
                    else:
                        doc.redo()
                    done += 1
                except Exception as e:
                    FreeCAD.Console.PrintWarning(
                        f"CADPilot: {'undo' if undo else 'redo'} stopped: {e}\n"
                    )
                    break
            with contextlib.suppress(Exception):
                doc.recompute()
            try:
                stack_after = list(getattr(doc, stack_attr, []) or [])
            except Exception:
                stack_after = []
            return {
                "success": True,
                "count": done,
                "stack_before": stack_before,
                "stack_after": stack_after,
                "objects": sorted(o.Name for o in doc.Objects),
            }

        res = dispatch_to_gui(task)
        if isinstance(res, dict):
            return res
        return _err(res)

    def undo_transactions(self, doc_name: str, n: int = 1) -> dict[str, Any]:
        """Undo the n most recent document transactions (session rollback)."""
        return self._undo_redo(doc_name, n, undo=True)

    def redo_transactions(self, doc_name: str, n: int = 1) -> dict[str, Any]:
        """Redo n previously undone transactions (only valid until a new op)."""
        return self._undo_redo(doc_name, n, undo=False)

    def save_document(self, doc_name: str, path: str | None = None) -> dict[str, Any]:
        """Save a document (saveAs when path is given)."""

        def task():
            try:
                doc = FreeCAD.getDocument(doc_name)
            except Exception:
                return f"Document '{doc_name}' not found."
            try:
                if path:
                    doc.saveAs(path)
                else:
                    if not doc.FileName:
                        return f"Document '{doc_name}' has never been saved; provide a path."
                    doc.save()
                FreeCAD.Console.PrintMessage(f"Document '{doc_name}' saved to '{doc.FileName}'.\n")
                return {"success": True, "file_name": doc.FileName}
            except Exception as e:
                return str(e)

        res = dispatch_to_gui(task)
        if isinstance(res, dict):
            return res
        return _err(res)

    def inspect_freecad(
        self,
        doc_name: str | None = None,
        obj_name: str | None = None,
        dotted_name: str | None = None,
    ) -> dict[str, Any]:
        """Runtime introspection: an object's properties/methods, or a FreeCAD
        API entry's docstring (e.g. dotted_name='Part.makeLoft')."""
        res = dispatch_to_gui(lambda: self._inspect_freecad_gui(doc_name, obj_name, dotted_name))
        if isinstance(res, dict):
            return res
        return _err(res)

    # --- read-only geometry sensing ------------------------------------------

    def measure_geometry(self, doc_name, obj_name):
        return dispatch_to_gui(lambda: _measure_geometry(doc_name, obj_name))

    def get_topology(self, doc_name, obj_name, element="faces", limit=50, offset=0):
        return dispatch_to_gui(lambda: _get_topology(doc_name, obj_name, element, limit, offset))

    def check_interference(self, doc_name, obj_a, obj_b):
        return dispatch_to_gui(lambda: _check_interference(doc_name, obj_a, obj_b))

    def get_positioning_info(self, doc_name, obj_name, element, element_index):
        """Return detailed global-coordinate spatial info for a specific face/edge/vertex."""
        return dispatch_to_gui(
            lambda: _get_positioning_info(doc_name, obj_name, element, element_index)
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
        """Compute and apply Placement to align obj's element to target's element."""

        # align_shapes mutates the object's Placement, so wrap in a transaction
        def task():
            return _align_shapes(
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

        return self._run_op_with_screenshot(
            task,
            {"success": True},
            None,  # no screenshot for now
            doc_name=doc_name,
            transaction=f"CADPilot: align_shapes {obj_name}",
        )

    # --- assembly toolchain ---------------------------------------------------

    def get_anchors(self, doc_name, obj_name):
        return dispatch_to_gui(lambda: _get_anchors(doc_name, obj_name))

    def set_anchors(
        self,
        doc_name,
        obj_name,
        anchors,
        replace=False,
        coord_frame="local",
        screenshot: dict | None = None,
    ):
        def task():
            return _set_anchors(doc_name, obj_name, anchors, replace, coord_frame)

        return self._run_op_with_screenshot(
            task,
            {"success": True},
            screenshot,
            doc_name=doc_name,
            transaction=f"CADPilot: set_anchors {obj_name}",
        )

    def assemble(
        self, doc_name, mates, tolerance=0.1, stop_on_error=True, screenshot: dict | None = None
    ):
        def task():
            return _assemble(doc_name, mates, tolerance, stop_on_error)

        return self._run_op_with_screenshot(
            task,
            {"success": True},
            screenshot,
            doc_name=doc_name,
            transaction="CADPilot: assemble",
            # commit whenever at least one mate passed (mirrors batch semantics)
            commit_if=lambda res: isinstance(res, dict) and res.get("passed", 0) > 0,
        )

    def verify_assembly(
        self, doc_name, checks=None, float_threshold=1.0, interference_min_volume=1.0
    ):
        return dispatch_to_gui(
            lambda: _verify_assembly(doc_name, checks, float_threshold, interference_min_volume)
        )

    _INSPECT_MAX_MEMBERS = 40

    def _inspect_freecad_gui(self, doc_name, obj_name, dotted_name):
        import importlib

        if obj_name:
            try:
                doc = FreeCAD.getDocument(doc_name)
            except Exception:
                return f"Document '{doc_name!r}' not found."
            obj = doc.getObject(obj_name)
            if obj is None:
                return f"Object '{obj_name}' not found in document '{doc_name}'."
            properties = {}
            for p in obj.PropertiesList:
                try:
                    properties[p] = obj.getTypeIdOfProperty(p)
                except Exception:
                    properties[p] = "unknown"
            methods = [m for m in dir(obj) if not m.startswith("_")][: self._INSPECT_MAX_MEMBERS]
            return {
                "success": True,
                "kind": "object",
                "name": obj.Name,
                "type_id": obj.TypeId,
                "properties": properties,
                "methods": methods,
                "doc": (obj.__doc__ or "").strip()[:800],
            }
        if dotted_name:
            parts = dotted_name.split(".")
            if len(parts) < 2:
                return f"dotted_name must look like 'Part.makeLoft', got {dotted_name!r}"
            try:
                target = importlib.import_module(parts[0])
                for p in parts[1:]:
                    target = getattr(target, p)
            except Exception as e:
                return f"Cannot resolve {dotted_name!r}: {e}"
            info = {
                "success": True,
                "kind": "api",
                "name": dotted_name,
                "doc": (getattr(target, "__doc__", "") or "").strip()[:1200],
            }
            if not callable(target):
                info["members"] = [m for m in dir(target) if not m.startswith("_")][
                    : self._INSPECT_MAX_MEMBERS
                ]
            return info
        return "Provide obj_name (with doc_name) or dotted_name."

    # --- code execution -----------------------------------------------------

    def execute_code_async(self, code: str) -> dict[str, Any]:
        """Start code execution in a background thread and return immediately.

        Use for long-running OCCT operations (fuse/cut/loft) that would otherwise
        exceed the CADPilot timeout. Returns a task_id; poll get_task_result(task_id)
        for status ("running"/"done"/"error"), output captured via task_print(),
        and the traceback on failure.
        """
        task_id = uuid.uuid4().hex[:8]
        with _async_tasks_lock:
            if len(_async_tasks) >= _ASYNC_TASKS_MAX:
                # Evict only FINISHED tasks (FIFO). Dropping a "running" entry
                # would orphan it: the worker could never publish its result
                # and get_task_result would report "unknown task_id".
                for old_id, old_entry in list(_async_tasks.items()):
                    if len(_async_tasks) < _ASYNC_TASKS_MAX:
                        break
                    if old_entry["status"] != "running":
                        del _async_tasks[old_id]
                if len(_async_tasks) >= _ASYNC_TASKS_MAX:
                    return {
                        "success": False,
                        "error": f"too many background tasks running "
                        f"(max {_ASYNC_TASKS_MAX}); wait for some to finish",
                    }
            _async_tasks[task_id] = {"status": "running", "output": "", "error": None}

        def task_print(*args, sep=" ", end="\n"):
            with _async_tasks_lock:
                entry = _async_tasks.get(task_id)
                if entry is not None:
                    entry["output"] += sep.join(str(a) for a in args) + end

        def _set_status(msg):
            dispatch_to_gui(lambda: FreeCADGui.getMainWindow().statusBar().showMessage(msg))

        def _clear_status():
            dispatch_to_gui(lambda: FreeCADGui.getMainWindow().statusBar().clearMessage())

        def worker() -> None:
            # NOTE: we do NOT redirect sys.stdout here. contextlib.redirect_stdout
            # swaps stdout process-wide, not per-thread, so it would race with the
            # GUI thread and other concurrent work. Background code reports via
            # task_print() (captured for get_task_result) or FreeCAD.Console
            # (which is thread-safe).
            exec_globals = {**globals(), "task_print": task_print}
            status, error = "done", None
            try:
                exec(code, exec_globals)
                FreeCAD.Console.PrintMessage(f"Async task {task_id} completed.\n")
            except Exception as e:
                status = "error"
                error = f"{e}\n{traceback.format_exc()}"
                FreeCAD.Console.PrintError(f"Async task {task_id} error: {error}\n")
            with _async_tasks_lock:
                entry = _async_tasks.get(task_id)
                if entry is not None:
                    entry["status"] = status
                    entry["error"] = error
            _clear_status()

        _set_status(f"CADPilot: running background task {task_id}…")
        threading.Thread(target=worker, daemon=True).start()
        return {
            "success": True,
            "task_id": task_id,
            "message": f"Code execution started in background (task {task_id}).",
        }

    def get_task_result(self, task_id: str) -> dict[str, Any]:
        """Return the status/output/error of an execute_code_async task."""
        with _async_tasks_lock:
            entry = _async_tasks.get(task_id)
            if entry is None:
                return {"success": False, "error": f"unknown task_id: {task_id!r}"}
            return {"success": True, "task_id": task_id, **entry}

    def execute_code(self, code: str, screenshot: dict | None = None) -> dict[str, Any]:
        """Execute Python code on the GUI thread and wait for the result.

        Runs on the GUI thread so that FreeCAD document operations
        (addObject, recompute, save) are safe and correctly ordered.
        Use execute_code_async for heavy OCCT boolean ops (fuse/cut)
        that would block the GUI thread too long.

        When ``screenshot`` params are given and the code succeeds, the
        screenshot is captured in the same GUI dispatch — no race with
        intervening ops.
        """
        output_buffer = io.StringIO()

        # Capture the screenshot in the same GUI dispatch if requested (single
        # RPC round trip, no race with intervening ops). execute_code is not
        # transactional (user code manages its own transactions).
        tmp_path = _make_tmp_png() if screenshot is not None else None

        def combined_task():
            # Run user code in a COPY of the module namespace (same as
            # execute_code_async): assignments in user code must not leak into
            # and corrupt this RPC module's globals across calls.
            exec_globals = {**globals()}
            try:
                with contextlib.redirect_stdout(output_buffer):
                    exec(code, exec_globals)
            except Exception:
                raise
            # Capture screenshot in the same GUI dispatch if requested
            if tmp_path is not None:
                shot = save_active_screenshot(
                    tmp_path,
                    screenshot.get("view_name", "Isometric"),
                    screenshot.get("width"),
                    screenshot.get("height"),
                    screenshot.get("focus_object"),
                )
                return True, tmp_path if shot is True else None
            return True, None

        try:
            out = dispatch_to_gui(combined_task, timeout=self.EXECUTE_CODE_TIMEOUT)
            if isinstance(out, tuple) and len(out) >= 2:
                res, shot_path = out[0], out[1]
            else:
                # Timeout or error from dispatch layer
                code_preview = code if len(code) <= 800 else code[:800] + "\n...(truncated)"
                FreeCAD.Console.PrintError(
                    f"Error executing Python code: {out}\n"
                    f"--- code ---\n{code_preview}\n--- end ---\n"
                )
                return _err(out)
            if _ok(res):
                FreeCAD.Console.PrintMessage("Python code executed successfully.\n")
                result = {
                    "success": True,
                    "message": "Python code executed successfully.\nOutput: "
                    + output_buffer.getvalue(),
                }
                if shot_path is not None:
                    b64 = _read_b64(shot_path)
                    if b64:
                        result["screenshot"] = b64
                return result
            # Log the offending code (truncated) to make errors traceable
            code_preview = code if len(code) <= 800 else code[:800] + "\n...(truncated)"
            FreeCAD.Console.PrintError(
                f"Error executing Python code: {res}\n--- code ---\n{code_preview}\n--- end ---\n"
            )
            return _err(res)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    # --- read-only queries (dispatched to the GUI thread: FreeCAD document
    # access is not thread-safe, even for reads) ------------------------------

    def get_objects(self, doc_name):
        res = dispatch_to_gui(lambda: self._get_objects_gui(doc_name))
        if isinstance(res, tuple):
            return {"success": True, "objects": res[1]}
        FreeCAD.Console.PrintWarning(f"CADPilot: get_objects failed: {res}\n")
        return {"success": False, "error": str(res), "objects": []}

    def get_object(self, doc_name, obj_name):
        res = dispatch_to_gui(lambda: self._get_object_gui(doc_name, obj_name))
        if isinstance(res, tuple):
            return {"success": True, "object": res[1]}
        FreeCAD.Console.PrintWarning(f"CADPilot: get_object failed: {res}\n")
        return {"success": False, "error": str(res), "object": None}

    def list_documents(self):
        res = dispatch_to_gui(lambda: list(FreeCAD.listDocuments().keys()))
        if isinstance(res, list):
            return {"success": True, "documents": res}
        FreeCAD.Console.PrintWarning(f"CADPilot: list_documents failed: {res}\n")
        return {"success": False, "error": str(res), "documents": []}

    def _get_objects_gui(self, doc_name):
        # FreeCAD.getDocument raises (not returns None) for an unknown name.
        # Report it as an error: silently returning [] made "document does not
        # exist" indistinguishable from "document is empty".
        try:
            doc = FreeCAD.getDocument(doc_name)
        except Exception:
            return f"Document '{doc_name}' not found."
        return (True, [serialize_object(obj) for obj in doc.Objects])

    def _get_object_gui(self, doc_name, obj_name):
        try:
            doc = FreeCAD.getDocument(doc_name)
        except Exception:
            return f"Document '{doc_name}' not found."
        obj = doc.getObject(obj_name)
        return (True, serialize_object(obj) if obj else None)

    def get_active_screenshot(
        self,
        view_name: str = "Isometric",
        width: int | None = None,
        height: int | None = None,
        focus_object: str | None = None,
    ) -> str | None:
        """Get a screenshot of the active view as a base64-encoded PNG string.

        Returns None if the active view does not support screenshots
        (e.g., TechDraw or Spreadsheet workbench).
        """
        tmp_path = _make_tmp_png()

        def task():
            try:
                active_view = FreeCADGui.ActiveDocument.ActiveView
            except Exception:
                return False
            if active_view is None or not hasattr(active_view, "saveImage"):
                view_type = type(active_view).__name__ if active_view is not None else "None"
                FreeCAD.Console.PrintWarning(
                    f"CADPilot: view type '{view_type}' does not support screenshots\n"
                )
                return False
            return save_active_screenshot(tmp_path, view_name, width, height, focus_object)

        try:
            res = dispatch_to_gui(task)
            if _ok(res):
                return _read_b64(tmp_path)
            if res is False:
                return None
            FreeCAD.Console.PrintWarning(f"CADPilot: screenshot failed: {res}\n")
            return None
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    # --- GUI-thread handlers --------------------------------------------------

    def _create_document_gui(self, name):
        doc = FreeCAD.newDocument(name)
        doc.recompute()
        FreeCAD.Console.PrintMessage(f"Document '{doc.Name}' created via RPC.\n")
        return {"success": True, "document_name": doc.Name}

    def _create_object_gui(self, doc_name, obj: Object, recompute: bool = True):
        return create_object_gui(doc_name, obj, recompute=recompute)

    def _edit_object_gui(self, doc_name: str, obj: Object):
        try:
            doc = FreeCAD.getDocument(doc_name)
        except Exception:
            FreeCAD.Console.PrintError(f"Document '{doc_name}' not found.\n")
            return f"Document '{doc_name}' not found.\n"

        obj_ins = doc.getObject(obj.name)
        if not obj_ins:
            FreeCAD.Console.PrintError(f"Object '{obj.name}' not found in document '{doc_name}'.\n")
            return f"Object '{obj.name}' not found in document '{doc_name}'.\n"

        try:
            has_expressions = any(
                isinstance(v, str) and v.startswith("=") for v in obj.properties.values()
            )
            set_object_property(doc, obj_ins, obj.properties)
            doc.recompute()
            if has_expressions and "Invalid" in [str(s) for s in obj_ins.State]:
                return (
                    f"Expression(s) on '{obj.name}' failed to evaluate "
                    "(check expression syntax and referenced cells/objects)."
                )
            FreeCAD.Console.PrintMessage(f"Object '{obj.name}' updated via RPC.\n")
            return True
        except Exception as e:
            return str(e)

    def _delete_object_gui(self, doc_name: str, obj_name: str):
        try:
            doc = FreeCAD.getDocument(doc_name)
        except Exception:
            FreeCAD.Console.PrintError(f"Document '{doc_name}' not found.\n")
            return f"Document '{doc_name}' not found.\n"

        try:
            doc.removeObject(obj_name)
            doc.recompute()
            FreeCAD.Console.PrintMessage(f"Object '{obj_name}' deleted via RPC.\n")
            return True
        except Exception as e:
            return str(e)

    def _save_active_screenshot(
        self,
        save_path: str,
        view_name: str = "Isometric",
        width: int | None = None,
        height: int | None = None,
        focus_object: str | None = None,
    ):
        return save_active_screenshot(save_path, view_name, width, height, focus_object)


def start_rpc_server(port=9875):
    global rpc_server_thread, rpc_server_instance

    if rpc_server_instance:
        return "RPC Server already running."

    # A previous stop may still be draining an in-flight request off-thread;
    # binding before its server_close() would hit the old socket.
    if _stop_thread is not None and _stop_thread.is_alive():
        _stop_thread.join(timeout=5.0)
        if _stop_thread.is_alive():
            return (
                "RPC Server is still stopping (a request is draining); try again in a few seconds."
            )

    settings = load_settings()
    remote_enabled = settings.get("remote_enabled", False)
    allowed_ips = settings.get("allowed_ips", "127.0.0.1")

    host = "0.0.0.0" if remote_enabled else "127.0.0.1"

    rpc_server_instance = FilteredXMLRPCServer(
        (host, port), allowed_ips_str=allowed_ips, allow_none=True, logRequests=False
    )
    rpc_server_instance.register_instance(FreeCADRPC())

    def server_loop():
        FreeCAD.Console.PrintMessage(f"RPC Server started at {host}:{port}\n")
        if remote_enabled:
            FreeCAD.Console.PrintMessage(
                f"Remote connections enabled. Allowed IPs: {allowed_ips}\n"
            )
        rpc_server_instance.serve_forever()

    rpc_server_thread = threading.Thread(target=server_loop, daemon=True)
    rpc_server_thread.start()

    init_waker()
    QtCore.QTimer.singleShot(500, process_gui_tasks)

    msg = f"RPC Server started at {host}:{port}."
    if remote_enabled:
        msg += f" Allowed IPs: {allowed_ips}"
    return msg


def stop_rpc_server():
    global rpc_server_instance, rpc_server_thread, _stop_thread

    if not rpc_server_instance:
        return "RPC Server was not running."

    server = rpc_server_instance
    thread = rpc_server_thread
    rpc_server_instance = None
    rpc_server_thread = None

    request_shutdown()
    cleanup_waker()

    def _shutdown_and_close():
        # shutdown() blocks until serve_forever drains the in-flight request,
        # and that request may itself be waiting on dispatch_to_gui — running
        # this on the GUI thread (menu command) froze the UI for up to the
        # dispatch timeout. server_close() must always follow: without it the
        # listening socket stays bound and Stop -> Start fails with
        # EADDRINUSE (the restart the README asks for after changing Remote
        # Connections or Allowed IPs).
        try:
            server.shutdown()
            if thread is not None:
                thread.join(timeout=10.0)
                if thread.is_alive():
                    FreeCAD.Console.PrintWarning(
                        "CADPilot: server thread still draining a request; "
                        "socket closes when it finishes.\n"
                    )
        finally:
            server.server_close()
        FreeCAD.Console.PrintMessage("RPC Server stopped.\n")

    _stop_thread = threading.Thread(target=_shutdown_and_close, daemon=True)
    _stop_thread.start()
    return "RPC Server stopping…"


register_commands()
schedule_toggle_sync()
