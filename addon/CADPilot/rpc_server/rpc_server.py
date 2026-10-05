import base64
import contextlib
import io
import os
import sys
import tempfile
import threading
import time
import traceback
import uuid
from typing import Any

import FreeCAD
import FreeCADGui
import Part  # noqa: F401 - pre-imported for execute_code snippets (exec_snippet copies globals)
from PySide import QtCore

from rpc_server import dbglog, step_engine, step_journal, watchdog
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
from rpc_server.feature_ops import (
    FEATURE_TYPES,
    create_feature_gui,
    describe_feature_reply,
)
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
from rpc_server.joint_ops import assembly_op as _assembly_op
from rpc_server.object_factory import (
    create_object_gui,
    delete_object_gui,
    edit_object_gui,
    repair_body_tips,
)
from rpc_server.property_mapper import Object
from rpc_server.request_log import LoggedXMLRPCServer
from rpc_server.serialize import serialize_object
from rpc_server.settings import load_settings
from rpc_server.view_manager import save_active_screenshot

rpc_server_thread = None
rpc_server_instance = None
_stop_thread = None  # drains shutdown off the GUI thread; see stop_rpc_server

# ``App`` is FreeCAD's ubiquitous alias for the FreeCAD module, and snippets
# written against the GUI console or the docs use it constantly. Snippets get a
# COPY of this module's globals (exec_snippet), so module-level names here are
# pre-imported inside execute_code — FreeCAD, FreeCADGui, Part and App.
App = FreeCAD

# Background-task registry for execute_code_async / get_task_result.
# Insertion-ordered dict doubles as the FIFO eviction order.
_async_tasks: dict[str, dict] = {}
_async_tasks_lock = threading.Lock()
_ASYNC_TASKS_MAX = 50

# Why the last get_active_screenshot returned None. get_view cannot say in its
# return value (it is base64-or-None on the wire, and an old MCP server would
# feed a dict to the base64 decoder), so the reason rides out of band through
# get_last_screenshot_error.
_last_screenshot_error = ""


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


def exec_snippet(code: str, out=None) -> None:
    """Run a snippet in a COPY of this module's namespace.

    Assignments must not leak into (and corrupt) the RPC server's globals, and
    a journal-replayed snippet must see exactly the names it was written
    against: ``FreeCAD`` (also ``App``), ``FreeCADGui`` and ``Part`` are
    imported at module level here, so they are pre-imported for every snippet.
    Shared by the ``execute_code`` RPC and by journal replay.
    """
    ns = {**globals()}
    if out is None:
        exec(code, ns)
    else:
        with contextlib.redirect_stdout(out):
            exec(code, ns)


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
        journal: dict | None = None,
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

        ``journal`` describes a step to append to the document's step log when
        the transaction commits: ``{"operation", "label", "params", "atomic"}``.
        The record is written INSIDE the transaction so it commits with the
        model change — but note that it is NOT reverted by ``doc.undo()``
        (FreeCAD does not undo document-level properties); step_engine
        reconciles the log explicitly on rollback. After the commit the record
        is probed against the undo stack: an empty commit added no undo entry,
        so the record is downgraded to read-only (a transaction claim without
        a matching undo entry would make plan_rollback pop an EARLIER step's
        transaction).
        """
        tmp_path = _make_tmp_png() if screenshot is not None else None

        def task():
            doc = None
            if transaction:
                with contextlib.suppress(Exception):
                    doc = FreeCAD.getDocument(doc_name) if doc_name else FreeCAD.ActiveDocument
            # The target must be the App-active document across the whole
            # open/commit window: FreeCAD 1.1.4 records the undo entry on the
            # ACTIVE document's stack, so committing here while another
            # document was active parked a ghost entry there (and vice versa),
            # and a later rollback popped the ghost instead of its own step —
            # reporting success while the model never moved.
            with step_engine.active_document(doc if transaction else None):
                return _task_body(doc)

        def _task_body(doc):
            in_transaction = False
            if transaction and doc is not None:
                try:
                    doc.openTransaction(transaction)
                    in_transaction = True
                except Exception as e:
                    FreeCAD.Console.PrintWarning(f"CADPilot: cannot open transaction: {e}\n")
            token_before = None
            if in_transaction:
                with contextlib.suppress(Exception):
                    token_before = step_engine.undo_token(doc)
            objects_before = sorted(o.Name for o in doc.Objects) if doc is not None else []
            try:
                # Machine-driven writes stay silent for the manual-edit sync
                # observer: this op's change is its own journal step.
                with step_engine.engine_quiet():
                    res = gui_fn()
            except Exception:
                if in_transaction:
                    doc.abortTransaction()
                raise
            ok = res is True or (isinstance(res, dict) and res.get("success"))
            should_commit = ok if commit_if is None else bool(commit_if(res))
            objects = None
            if in_transaction:
                if should_commit:
                    objects = sorted(o.Name for o in doc.Objects) if doc is not None else []
                    rec = None
                    if journal and doc is not None:
                        rec = step_engine.record_commit(
                            doc,
                            operation=journal.get("operation", "unknown"),
                            label=journal.get("label", ""),
                            params=journal.get("params"),
                            transaction=transaction,
                            atomic=bool(journal.get("atomic", True)),
                            executable=journal.get("executable"),
                            objects_before=objects_before,
                            objects_after=objects,
                        )
                    doc.commitTransaction()
                    step_engine.downgrade_if_no_undo(doc, rec, token_before)
                    dbglog.get_logger("tx").info("committed transaction %r", transaction)
                else:
                    doc.abortTransaction()
                    dbglog.get_logger("tx").info("aborted transaction %r", transaction)
            # Whenever a transaction was committed the document changed, so the
            # caller needs the fingerprint even for partial batch failures —
            # otherwise the session log and the undo stack would desync.
            if not should_commit:
                return res, None, in_transaction
            # Resolve the document for the fingerprint even when the op itself
            # created it (defensive: no caller passes doc_name=None today).
            if doc is None:
                doc = FreeCAD.ActiveDocument
            if objects is None:
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
            journal={
                "operation": "create_object",
                "label": step_journal.step_label(
                    "create",
                    obj.name,
                    description=obj_data.get("description"),
                    detail=f"({obj.type})",
                ),
                "params": {
                    "obj_name": obj.name,
                    "obj_type": obj.type,
                    "obj_properties": obj.properties,
                },
            },
        )

    def create_feature(self, doc_name, feature_spec: dict, screenshot: dict | None = None):
        # The caller's one-line intent is presentation, not geometry — keep it
        # out of the spec the builders and describe_feature are handed.
        spec = {k: v for k, v in feature_spec.items() if k != "description"}
        ftype = str(spec.get("type", "feature"))
        base = spec.get("base")

        def task():
            try:
                doc = FreeCAD.getDocument(doc_name)
            except Exception:
                return f"Document '{doc_name}' not found."
            try:
                feat = create_feature_gui(doc, spec)
                FreeCAD.Console.PrintMessage(
                    f"Feature '{feat.Name}' ({ftype}) created in '{doc_name}' via RPC.\n"
                )
                extra = describe_feature_reply(feat, spec)
                return {"success": True, "object_name": feat.Name, **extra}
            except Exception as e:
                return str(e)

        return self._run_op_with_screenshot(
            task,
            {"success": True},
            screenshot,
            doc_name=doc_name,
            transaction=f"CADPilot: {ftype} {base or ''}",
            journal={
                "operation": ftype,
                "label": step_journal.step_label(
                    ftype, base, spec, description=feature_spec.get("description")
                ),
                "params": {
                    "obj_name": base,
                    "obj_properties": {k: v for k, v in spec.items() if k not in ("type", "base")},
                },
            },
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
            journal={
                "operation": "assembly",
                "label": f"assembly {spec.get('operation', 'op')}",
                # The full spec is the replay payload: start/add_component/
                # mate/solve/unmate/rollback_step are all data-driven and
                # re-runnable; a read-only verify is downgraded by the
                # empty-commit probe in _run_op_with_screenshot.
                "params": {"spec": spec},
            },
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
            journal={
                "operation": "edit_object",
                "label": step_journal.step_label(
                    "edit", obj.name, description=properties.get("description")
                ),
                "params": {"obj_name": obj.name, "obj_properties": obj.properties},
            },
        )

    def delete_object(self, doc_name: str, obj_name: str, screenshot: dict | None = None):
        return self._run_op_with_screenshot(
            lambda: self._delete_object_gui(doc_name, obj_name),
            {"success": True, "object_name": obj_name},
            screenshot,
            doc_name=doc_name,
            transaction=f"CADPilot: delete_object {obj_name}",
            journal={
                "operation": "delete_object",
                "label": f"delete '{obj_name}'",
                "params": {"obj_name": obj_name},
            },
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
            doc = None
            with contextlib.suppress(Exception):
                doc = FreeCAD.getDocument(doc_name)
            for op in ops:
                before = [o.Name for o in doc.Objects] if doc is not None else []
                res = self._run_one_operation(doc_name, op, is_batch=True)
                if doc is not None and not res.get("success"):
                    # A sub-op can fail AFTER creating its object (a bad
                    # property value, a later validation step). Those
                    # half-built objects used to commit with the batch — live,
                    # a failed pad left a valid default-length Pad in the
                    # document while its own per-op result said success:false.
                    # Remove them (reverse creation order) inside the batch's
                    # transaction, so the failure leaves no debris.
                    before_set = set(before)
                    debris = [o.Name for o in doc.Objects if o.Name not in before_set]
                    for name in reversed(debris):
                        with contextlib.suppress(Exception):
                            doc.removeObject(name)
                    if debris:
                        # Removing the tip of a Body would leave it Invalid for
                        # every later feature (live: a failed thickness took the
                        # Tip with it and the next dress-up refused).
                        with contextlib.suppress(Exception):
                            if repair_body_tips(doc):
                                doc.recompute()
                        res["removed_debris"] = debris
                        res.setdefault(
                            "warning",
                            "new object(s) created by this failed sub-op were removed; "
                            "property writes it already applied to existing objects stay",
                        )
                results.append(res)
                if stop_on_error and not res["success"]:
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
            journal={
                "operation": "batch",
                "label": step_journal.batch_label(ops),
                "params": {"ops": ops},
            },
        )

    def _run_one_operation(self, doc_name: str, op, is_batch: bool = False) -> dict:
        """Run a single batch op on the GUI thread; returns a per-op result dict.

        When ``is_batch`` is True, create_object skips per-object recompute
        (the batch handler does one recompute after all ops).
        """
        # Both key conventions must work here: RPC batch ops carry "action",
        # journal-native steps carry "operation" (mirrors sj.sub_operation).
        action = (op.get("action") or op.get("operation")) if isinstance(op, dict) else None
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
                        **{
                            k: v
                            for k, v in (op.get("obj_properties") or {}).items()
                            if k != "description"
                        },
                    }
                    feat = create_feature_gui(doc, spec)
                    # Same payload the single-op path returns. Without dof/
                    # warnings a batch-built sketch reported bare success while
                    # it was under-constrained, and a pocket that cut nothing
                    # said nothing — the batch path used to drop both.
                    res = {
                        "success": True,
                        "object_name": feat.Name,
                        **describe_feature_reply(feat, spec),
                    }
                except Exception as e:
                    return {"success": False, "action": action, "error": str(e)}
            else:
                if action is None:
                    return {
                        "success": False,
                        "action": None,
                        "error": "sub-op has no 'action' (or 'operation') key",
                    }
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
            except Exception:
                stack_before = []
            # step_engine owns the loop: it never calls undo()/redo() blind past
            # the end of the stack, skips ghost entries owned by other
            # documents, and reports the count that actually went.
            res = step_engine.undo_n(doc, n) if undo else step_engine.redo_n(doc, n)
            try:
                stack_after = list(getattr(doc, stack_attr, []) or [])
            except Exception:
                stack_after = []
            count = res.get("count", 0)
            out = {
                "success": True,
                "count": count,
                "ghosts_skipped": res.get("ghosts_skipped", 0),
                "stack_before": stack_before,
                "stack_after": stack_after,
                "objects": res.get("objects", []),
            }
            if not undo and n > 0 and count == 0:
                # Nothing to redo means the caller's redo buffer and FreeCAD's
                # redo stack have diverged (another actor consumed it, or a
                # reopen emptied it). Reporting success with an empty result
                # made session_redo a silent no-op that never drained.
                out["success"] = False
                out["error"] = (
                    "nothing to redo: FreeCAD's redo stack holds no transaction for this "
                    "document — it was consumed or cleared by other work, so the caller's "
                    "redo history and the document are out of sync."
                )
            return out

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

    # --- step journal (steps panel / staged execution) -----------------------

    def get_step_journal(self, doc_name: str) -> dict[str, Any]:
        """Read the document's step journal (read-only, no transaction)."""

        def task():
            try:
                doc = FreeCAD.getDocument(doc_name)
            except Exception:
                return f"Document '{doc_name}' not found."
            return step_engine.apply_op(doc, {"operation": "status"})

        res = dispatch_to_gui(task)
        if isinstance(res, dict):
            return res
        return _err(res)

    def journal_op(self, doc_name: str, spec: dict) -> dict[str, Any]:
        """Step-journal operation: set_plan / run_* / rollback_to / reexecute.

        Each executed step opens its own transaction and writes the journal
        inside it, so FreeCAD's undo/redo keeps the log in lockstep.
        """

        def task():
            try:
                doc = FreeCAD.getDocument(doc_name)
            except Exception:
                return f"Document '{doc_name}' not found."
            return step_engine.apply_op(doc, spec or {})

        res = dispatch_to_gui(task)
        if isinstance(res, dict):
            return res
        return _err(res)

    # --- diagnostics ----------------------------------------------------------

    def get_addon_log(
        self,
        level: str | None = None,
        grep: str | None = None,
        since_seq: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Read this addon's in-memory debug log.

        Deliberately NOT dispatched to the GUI thread: the ring buffer is
        thread-safe and holds no FreeCAD state, so the log stays readable
        exactly when it is most needed — while the GUI thread is wedged and
        every other RPC is blocked behind it.
        """
        try:
            records = dbglog.query(level=level, grep=grep, since_seq=since_seq, limit=limit)
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
        return {"success": True, "records": records, "status": dbglog.status()}

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
            journal={
                "operation": "align_shapes",
                "label": f"align '{obj_name}' -> '{target_obj_name}'",
                "params": {
                    "obj_name": obj_name,
                    "element": element,
                    "element_index": element_index,
                    "target_obj_name": target_obj_name,
                    "target_element": target_element,
                    "target_element_index": target_element_index,
                    "mode": mode,
                    "offset": offset,
                },
            },
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
            journal={
                "operation": "set_anchors",
                "label": f"anchors on '{obj_name}'",
                "params": {
                    "obj_name": obj_name,
                    "anchors": anchors,
                    "replace": replace,
                    "coord_frame": coord_frame,
                },
            },
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
            journal={
                "operation": "assemble",
                "label": f"assemble {len(mates)} mate(s)",
                "params": {
                    "mates": mates,
                    "tolerance": tolerance,
                    "stop_on_error": stop_on_error,
                },
            },
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

    def execute_code(
        self, code: str, screenshot: dict | None = None, doc_name: str | None = None
    ) -> dict[str, Any]:
        """Execute Python code on the GUI thread and wait for the result.

        Runs on the GUI thread so that FreeCAD document operations
        (addObject, recompute, save) are safe and correctly ordered.
        Use execute_code_async for heavy OCCT boolean ops (fuse/cut)
        that would block the GUI thread too long.

        With ``doc_name`` the run is BOUND to that document: it becomes the
        active document (so ``App.ActiveDocument`` inside the snippet
        resolves there), the wrapper transaction and the journal step land
        on it. Without it the active document at call time is used — which
        under two concurrent agents is whichever document the OTHER agent's
        call left active, so always pass doc_name (or work in a session,
        which binds it automatically).

        The snippet is wrapped in a FreeCAD transaction, so a document change
        it makes becomes a single undo entry the step journal can roll back,
        re-run or replay. A snippet that changes nothing (an inspection) leaves
        no undo entry and is recorded as a read-only step. The result carries
        ``changed`` so the caller knows which kind it was.

        When ``screenshot`` params are given and the code succeeds, the
        screenshot is captured in the same GUI dispatch — no race with
        intervening ops.
        """
        output_buffer = io.StringIO()

        # Capture the screenshot in the same GUI dispatch if requested (single
        # RPC round trip, no race with intervening ops).
        tmp_path = _make_tmp_png() if screenshot is not None else None

        def combined_task():
            doc = None
            if doc_name:
                # Bind first, fail fast: an unknown document must be an error,
                # not a silent fall-through to whatever else is active.
                try:
                    doc = FreeCAD.getDocument(doc_name)
                except Exception as e:
                    raise ValueError(f"unknown document {doc_name!r}: {e}") from e
                # App.setActiveDocument makes App.ActiveDocument resolve to the
                # bound document inside the snippet. There is no
                # Gui.activateDocument on 1.1.x, and flipping the foreground MDI
                # tab is deliberately NOT done — a concurrent agent holds it.
                with contextlib.suppress(Exception):
                    FreeCAD.setActiveDocument(doc_name)
            else:
                with contextlib.suppress(Exception):
                    doc = FreeCAD.ActiveDocument
            # Wrap the snippet in a transaction so its document changes become
            # exactly one undo entry (a bare property write is otherwise NOT
            # undoable at all). An empty commit adds no undo entry, so a
            # read-only inspection costs nothing. Transactions do not nest: a
            # snippet that opens its own merges into this one, and if one is
            # ALREADY open we leave it untouched — we cannot attribute its
            # changes — and record the step conservatively.
            wrapped = doc is not None and not doc.HasPendingTransaction
            tx_name = None
            with contextlib.suppress(Exception):
                tx_name = doc.Name if doc is not None else None
            token_before = None
            if wrapped:
                with contextlib.suppress(Exception):
                    token_before = step_engine.undo_token(doc)
            before = step_engine.object_names(doc)
            # The snippet is free to switch documents (a documented move: set the
            # active document so the step lands on the right one) or to close
            # one, so "what changed" must come from the whole open set — the old
            # code attributed every change to whatever document was active when
            # the call ARRIVED, which both lied in the reply ("read-only: no
            # document change" for a run that did mutate) and filed the step
            # into the wrong document's journal.
            docs_before = step_engine.document_tokens()
            if wrapped:
                doc.openTransaction("CADPilot: execute_code")
            try:
                # Muted like every RPC mutation: the snippet's change is its
                # own journal step, not a manual edit on an earlier one.
                with step_engine.engine_quiet():
                    exec_snippet(code, output_buffer)
            except BaseException:
                # A failing snippet must not leave half-applied mutations.
                if wrapped:
                    with contextlib.suppress(Exception):
                        doc.abortTransaction()
                raise
            tx_changed = False
            if wrapped:
                with contextlib.suppress(Exception):
                    doc.commitTransaction()  # empty -> no undo entry
                    tx_changed = step_engine.undo_token(doc) != token_before
            changed_docs = step_engine.changed_documents(docs_before)
            # The step belongs to the document whose transaction we own, and is
            # atomic only when THAT document changed. A change made to another
            # document happened outside our transaction: it owns its own undo
            # entry there, so it must not be recorded as this step (nor as this
            # document's session step).
            foreign = [n for n in changed_docs if n != tx_name]
            document = tx_name if (tx_name in changed_docs or not changed_docs) else changed_docs[0]
            # A snippet that changed the document is ATOMIC and replayable;
            # a read-only one stays non-atomic, so rollback neither stops at it
            # nor re-runs it.
            try:
                if doc is not None:
                    step_engine.append_execute_code(
                        doc,
                        code=code,
                        changed=tx_changed,
                        objects_before=before,
                    )
            except Exception:
                pass
            # Capture screenshot in the same GUI dispatch if requested
            # The snippet may have closed or replaced the active document (both
            # are legitimate: closing a scratch doc, or setting the active doc so
            # the step lands on the right one) — reading .Name off the deleted
            # reference raises ReferenceError and would report a snippet that
            # SUCCEEDED as a failure.
            if tmp_path is not None:
                shot = save_active_screenshot(
                    tmp_path,
                    screenshot.get("view_name", "Isometric"),
                    screenshot.get("width"),
                    screenshot.get("height"),
                    screenshot.get("focus_object"),
                    doc_name=doc_name,
                )
                return (
                    True,
                    tmp_path if shot is True else None,
                    bool(changed_docs),
                    document,
                    wrapped,
                    foreign,
                )
            return True, None, bool(changed_docs), document, wrapped, foreign

        try:
            out = dispatch_to_gui(combined_task, timeout=self.EXECUTE_CODE_TIMEOUT)
            if isinstance(out, tuple) and len(out) >= 2:
                res, shot_path = out[0], out[1]
                changed = out[2] if len(out) > 2 else False
                # Which document actually changed (not "which was active"), and
                # whether the change was inside OUR transaction.
                changed_doc = out[3] if len(out) > 3 else None
                attributed = out[4] if len(out) > 4 else True
                foreign = out[5] if len(out) > 5 else []
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
                    "changed": bool(changed),
                    "document": changed_doc,
                    # False when a transaction was already open, so the change
                    # cannot be attributed to a transaction we own: the step is
                    # rollback-able only by whatever owns that transaction.
                    "attributed": bool(attributed),
                    "foreign_changes": list(foreign),
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
        doc_name: str | None = None,
    ) -> str | None:
        """Get a screenshot of the active view as a base64-encoded PNG string.

        With ``doc_name`` the capture activates that document first, so a
        concurrent agent holding another tab in the foreground cannot swap
        the framed model.

        Returns None on ANY failure — the caller asks
        ``get_last_screenshot_error`` for the reason, because the bare None
        cannot distinguish "this view type cannot be captured" (TechDraw,
        Spreadsheet) from "the window was occluded", "the PNG could not be read
        back" or a GUI-dispatch timeout, and the old single message blamed the
        view type for all of them.
        """
        global _last_screenshot_error
        _last_screenshot_error = ""
        tmp_path = _make_tmp_png()

        def task():
            # Probe the SAME view the capture will use: the bound document's
            # own view when doc_name is given — a concurrent agent's foreground
            # tab may be a Spreadsheet/TechDraw, which must not fail OUR
            # capture — the foreground view otherwise.
            try:
                if doc_name:
                    gdoc = FreeCADGui.getDocument(doc_name)
                    active_view = gdoc.activeView() if gdoc is not None else None
                else:
                    active_view = FreeCADGui.ActiveDocument.ActiveView
            except Exception as e:
                global _last_screenshot_error
                _last_screenshot_error = f"the document's view could not be resolved ({e})"
                return False
            if active_view is None or not hasattr(active_view, "saveImage"):
                view_type = type(active_view).__name__ if active_view is not None else "None"
                _last_screenshot_error = (
                    f"the active view is '{view_type}', which has no saveImage — "
                    "TechDraw/Spreadsheet pages and other non-3D views cannot be captured"
                )
                FreeCAD.Console.PrintWarning(
                    f"CADPilot: view type '{view_type}' does not support screenshots\n"
                )
                return False
            res = save_active_screenshot(
                tmp_path, view_name, width, height, focus_object, doc_name=doc_name
            )
            if res is not True:
                _last_screenshot_error = f"the capture failed ({res or 'no detail'})"
            return res

        try:
            res = dispatch_to_gui(task)
            if _ok(res):
                b64 = _read_b64(tmp_path)
                if b64 is None:
                    _last_screenshot_error = (
                        "the PNG was written but could not be read back "
                        "(empty file or a permissions problem)"
                    )
                return b64
            if not _last_screenshot_error:
                if isinstance(res, dict) and res.get("error"):
                    _last_screenshot_error = str(res["error"])
                elif isinstance(res, str) and res:
                    _last_screenshot_error = res
            FreeCAD.Console.PrintWarning(
                f"CADPilot: screenshot failed: {_last_screenshot_error or res}\n"
            )
            return None
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def get_last_screenshot_error(self) -> str:
        """Why the last ``get_active_screenshot`` returned None ("" = none).

        Best-effort same-process diagnostic: call it immediately after a None
        to turn the generic "cannot get screenshot" into the actual cause.
        """
        return _last_screenshot_error

    # --- GUI-thread handlers --------------------------------------------------

    def _create_document_gui(self, name):
        doc = FreeCAD.newDocument(name)
        doc.recompute()
        FreeCAD.Console.PrintMessage(f"Document '{doc.Name}' created via RPC.\n")
        return {"success": True, "document_name": doc.Name}

    def _create_object_gui(self, doc_name, obj: Object, recompute: bool = True):
        return create_object_gui(doc_name, obj, recompute=recompute)

    def _edit_object_gui(self, doc_name: str, obj: Object):
        return edit_object_gui(doc_name, obj)

    def _delete_object_gui(self, doc_name: str, obj_name: str):
        return delete_object_gui(doc_name, obj_name)

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

    # A start attempt IS the intent to have the endpoint up: record it before
    # anything can fail, so the watchdog completes a start that this call
    # cannot (a drain-in-progress refusal, a stolen port at boot).
    watchdog.set_desired(True)

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

    dbglog.setup_logging()
    settings = load_settings()
    remote_enabled = settings.get("remote_enabled", False)
    allowed_ips = settings.get("allowed_ips", "127.0.0.1")

    host = "0.0.0.0" if remote_enabled else "127.0.0.1"

    rpc_server_instance = LoggedXMLRPCServer(
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


# Hot-reload order: dependencies BEFORE importers. importlib.reload does not
# rebind `from X import name` in the modules that imported X, so reloading an
# importer before its dependency silently keeps the OLD function alive (the
# property_mapper trap). tests/test_addon_gui_wiring.py pins every top-level
# `from rpc_server.X import ...` against this list.
_RELOAD_ORDER = [
    "rpc_server.dbglog",
    "rpc_server.ip_filter",
    "rpc_server.settings",
    "rpc_server.property_mapper",
    "rpc_server.serialize",
    "rpc_server.sketcher_ops",
    "rpc_server.tip_policy",
    "rpc_server.step_journal",
    "rpc_server.geometry_query",
    "rpc_server.gui_dispatch",
    "rpc_server.trim_ops",
    "rpc_server.commands",
    "rpc_server.object_factory",
    "rpc_server.feature_ops",
    "rpc_server.assembly_ops",
    "rpc_server.request_log",
    "rpc_server.joint_ops",
    "rpc_server.view_manager",
    "rpc_server.step_engine",
    "rpc_server.watchdog",
    "rpc_server.step_panel",
]
_RESTART_MAX_ATTEMPTS = 60  # 1 s apart; then the watchdog takes over
_RESTART_SUPPRESS_S = _RESTART_MAX_ATTEMPTS + 30


def restart_rpc_server(port: int = 9875) -> dict:
    """Hot-restart the RPC server: stop, reload the addon modules, start again.

    Replaces the manual execute_code recipe. The reload happens HERE, in
    ``_RELOAD_ORDER`` (dependencies before importers, the main module last),
    and a reload error is reported but never aborts the restart. The start is
    deferred to a main-window QTimer because the in-flight RPC request (this
    call) blocks the old server's shutdown drain — the first attempt cannot
    succeed before this function returns. Every attempt resolves
    ``rpc_server.rpc_server`` FRESH, so it binds the reloaded code.

    desired is set True up front and the watchdog is suppressed for the
    restart window; if all attempts fail, the watchdog keeps retrying every
    3 s. Poll ``ping`` or read ``get_addon_log`` to see the endpoint return.
    """
    import importlib

    if time.time() < watchdog.suppress_until():
        return {"success": False, "error": "a restart is already in progress"}

    watchdog.set_desired(True)
    watchdog.suppress(_RESTART_SUPPRESS_S)

    result: dict = {
        "success": True,
        "stopped": stop_rpc_server(),
        "reloaded": [],
        "reload_errors": [],
    }

    names = [n for n in _RELOAD_ORDER if n in sys.modules]
    names += sorted(
        n
        for n in sys.modules
        if n.startswith("rpc_server.")
        and n not in _RELOAD_ORDER
        and n != "rpc_server.rpc_server"
        and not n.endswith(".__init__")
    )
    names.append("rpc_server.rpc_server")
    for name in names:
        try:
            importlib.reload(sys.modules[name])
            result["reloaded"].append(name)
        except Exception as e:
            result["reload_errors"].append(f"{name}: {e}")
            FreeCAD.Console.PrintError(f"CADPilot restart: reloading {name} failed: {e}\n")

    timer_holder: dict = {}

    attempts = 0

    def _cancel_timer():
        timer = timer_holder.get("timer")
        if timer is not None:
            timer.stop()
            timer.deleteLater()
            timer_holder["timer"] = None

    def _try_start():
        nonlocal attempts
        attempts += 1
        try:
            fresh = importlib.import_module("rpc_server.rpc_server")
            msg = str(fresh.start_rpc_server(port))
        except Exception as e:
            msg = f"start raised: {e}"
        if "started at" in msg or "already running" in msg:
            watchdog.suppress(0)
            dbglog.get_logger("rpc").info(
                "restart: RPC server is back after %d attempt(s)", attempts
            )
            _cancel_timer()
            return
        if attempts >= _RESTART_MAX_ATTEMPTS:
            # Hand the remaining retries to the watchdog: desired is True and
            # the suppress window is spent, so its 3 s tick keeps trying.
            watchdog.suppress(0)
            dbglog.get_logger("rpc").error(
                "restart: server did not come back after %d attempts (%s); "
                "the watchdog keeps retrying",
                attempts,
                msg,
            )
            _cancel_timer()

    timer = QtCore.QTimer(FreeCADGui.getMainWindow())
    timer.setInterval(1000)
    timer.timeout.connect(_try_start)
    timer_holder["timer"] = timer
    timer.start()

    result["note"] = (
        "start is deferred (the shutdown drain waits for this call to return); "
        f"up to {_RESTART_MAX_ATTEMPTS} attempts 1 s apart, then the watchdog takes over"
    )
    return result


register_commands()
schedule_toggle_sync()
