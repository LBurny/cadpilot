import http.client
import logging
import xmlrpc.client
from typing import Any

logger = logging.getLogger("CADPilot")

# Errors that mean the TCP connection is dead (FreeCAD restarted, addon
# restarted, socket reset). Retrying once on a fresh proxy is safe.
# socket.timeout is deliberately excluded: a timeout may mean FreeCAD is
# still executing the request, and retrying would double-execute it.
_RECOVERABLE_ERRORS = (ConnectionError, http.client.HTTPException)


class _TimeoutTransport(xmlrpc.client.Transport):
    """XML-RPC transport with a configurable socket timeout.

    The default Transport has no timeout, so a frozen FreeCAD GUI thread
    causes the MCP client to hang indefinitely (observed: 4+ minute waits).
    """

    def __init__(self, timeout: float = 30, **kwargs):
        super().__init__(**kwargs)
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


class FreeCADConnection:
    def __init__(self, host: str = "localhost", port: int = 9875, timeout: float = 150):
        self._uri = f"http://{host}:{port}"
        self._timeout = timeout
        self.server = self._make_proxy(timeout)

    def _make_proxy(self, timeout: float) -> xmlrpc.client.ServerProxy:
        return xmlrpc.client.ServerProxy(
            self._uri,
            allow_none=True,
            transport=_TimeoutTransport(timeout=timeout),
        )

    def _invoke(self, method: str, *args):
        """Call an RPC method, rebuilding the proxy and retrying once if the
        connection died (e.g. FreeCAD or the addon was restarted)."""
        try:
            return getattr(self.server, method)(*args)
        except _RECOVERABLE_ERRORS as e:
            logger.warning(
                f"RPC connection lost during '{method}' ({e}); reconnecting and retrying once"
            )
            self.server = self._make_proxy(self._timeout)
            return getattr(self.server, method)(*args)

    def _invoke_with_screenshot(self, method: str, *args, screenshot: dict[str, Any] | None):
        """Call a mutation RPC with an inline screenshot request.

        Falls back to the legacy two-call path (op + get_active_screenshot)
        when the addon predates the screenshot parameter, so a new MCP server
        keeps working against an old addon install.
        """
        if screenshot is None:
            return self._invoke(method, *args)
        try:
            return self._invoke(method, *args, screenshot)
        except xmlrpc.client.Fault as e:
            if "TypeError" not in str(e):
                raise
            logger.info(
                f"Addon does not support inline screenshots for '{method}'; using legacy path"
            )
            res = self._invoke(method, *args)
            if isinstance(res, dict) and res.get("success"):
                shot = self.get_active_screenshot(
                    screenshot.get("view_name", "Isometric"),
                    screenshot.get("width"),
                    screenshot.get("height"),
                    screenshot.get("focus_object"),
                )
                if shot:
                    res["screenshot"] = shot
            return res

    def disconnect(self) -> None:
        # Transport.close() clears cached HTTP connections if one was opened.
        transport = getattr(self.server, "_ServerProxy__transport", None)
        close = getattr(transport, "close", None)
        if callable(close):
            close()

    def ping(self) -> bool:
        return self._invoke("ping")

    def create_document(
        self, name: str, screenshot: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._invoke_with_screenshot("create_document", name, screenshot=screenshot)

    def create_object(
        self,
        doc_name: str,
        obj_data: dict[str, Any],
        screenshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._invoke_with_screenshot(
            "create_object", doc_name, obj_data, screenshot=screenshot
        )

    def create_feature(
        self,
        doc_name: str,
        spec: dict[str, Any],
        screenshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._invoke_with_screenshot("create_feature", doc_name, spec, screenshot=screenshot)

    def assembly_op(self, doc_name: str, spec: dict[str, Any]) -> dict[str, Any]:
        """Assembly-session RPC (addon joint_ops.assembly_op). No screenshots."""
        return self._invoke("assembly_op", doc_name, spec)

    def edit_object(
        self,
        doc_name: str,
        obj_name: str,
        obj_data: dict[str, Any],
        screenshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._invoke_with_screenshot(
            "edit_object", doc_name, obj_name, obj_data, screenshot=screenshot
        )

    def delete_object(
        self,
        doc_name: str,
        obj_name: str,
        screenshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._invoke_with_screenshot(
            "delete_object", doc_name, obj_name, screenshot=screenshot
        )

    def execute_code(self, code: str, screenshot: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._invoke_with_screenshot("execute_code", code, screenshot=screenshot)

    def execute_code_async(self, code: str) -> dict[str, Any]:
        return self._invoke("execute_code_async", code)

    def get_task_result(self, task_id: str) -> dict[str, Any]:
        return self._invoke("get_task_result", task_id)

    def execute_operations(
        self,
        doc_name: str,
        ops: list[dict[str, Any]],
        stop_on_error: bool = False,
        screenshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._invoke_with_screenshot(
            "execute_operations", doc_name, ops, stop_on_error, screenshot=screenshot
        )

    def undo_transactions(self, doc_name: str, n: int = 1) -> dict[str, Any]:
        return self._invoke("undo_transactions", doc_name, n)

    def redo_transactions(self, doc_name: str, n: int = 1) -> dict[str, Any]:
        return self._invoke("redo_transactions", doc_name, n)

    def save_document(self, doc_name: str, path: str | None = None) -> dict[str, Any]:
        return self._invoke("save_document", doc_name, path)

    def inspect_freecad(
        self,
        doc_name: str | None = None,
        obj_name: str | None = None,
        dotted_name: str | None = None,
    ) -> dict[str, Any]:
        return self._invoke("inspect_freecad", doc_name, obj_name, dotted_name)

    def measure_geometry(self, doc_name: str, obj_name: str) -> dict[str, Any]:
        return self._invoke("measure_geometry", doc_name, obj_name)

    def get_topology(
        self,
        doc_name: str,
        obj_name: str,
        element: str = "faces",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self._invoke("get_topology", doc_name, obj_name, element, limit, offset)

    def check_interference(self, doc_name: str, obj_a: str, obj_b: str) -> dict[str, Any]:
        return self._invoke("check_interference", doc_name, obj_a, obj_b)

    def get_positioning_info(
        self,
        doc_name: str,
        obj_name: str,
        element: str,
        element_index: int,
    ) -> dict[str, Any]:
        return self._invoke("get_positioning_info", doc_name, obj_name, element, element_index)

    def align_shapes(
        self,
        doc_name: str,
        obj_name: str,
        element: str,
        element_index: int,
        target_obj_name: str,
        target_element: str,
        target_element_index: int,
        mode: str = "touch",
        offset: float = 0.0,
    ) -> dict[str, Any]:
        return self._invoke(
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

    def get_anchors(self, doc_name: str, obj_name: str) -> dict[str, Any]:
        return self._invoke("get_anchors", doc_name, obj_name)

    def set_anchors(
        self,
        doc_name: str,
        obj_name: str,
        anchors: dict[str, Any],
        replace: bool = False,
        coord_frame: str = "local",
        screenshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._invoke_with_screenshot(
            "set_anchors",
            doc_name,
            obj_name,
            anchors,
            replace,
            coord_frame,
            screenshot=screenshot,
        )

    def assemble(
        self,
        doc_name: str,
        mates: list[dict[str, Any]],
        tolerance: float = 0.1,
        stop_on_error: bool = True,
        screenshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._invoke_with_screenshot(
            "assemble", doc_name, mates, tolerance, stop_on_error, screenshot=screenshot
        )

    def verify_assembly(
        self,
        doc_name: str,
        checks: list[dict[str, Any]] | None = None,
        float_threshold: float = 1.0,
        interference_min_volume: float = 1.0,
    ) -> dict[str, Any]:
        return self._invoke(
            "verify_assembly", doc_name, checks, float_threshold, interference_min_volume
        )

    def get_active_screenshot(
        self,
        view_name: str = "Isometric",
        width: int | None = None,
        height: int | None = None,
        focus_object: str | None = None,
    ) -> str | None:
        try:
            return self._invoke("get_active_screenshot", view_name, width, height, focus_object)
        except Exception as e:
            logger.error(f"Error getting screenshot: {e}")
            return None

    def get_objects(self, doc_name: str) -> list[dict[str, Any]]:
        res = self._invoke("get_objects", doc_name)
        # New addon returns {"success": ..., "objects": [...]};
        # old addon returns the list directly. A failure (e.g. document not
        # found) must surface as an error, not as a misleading empty list.
        if isinstance(res, dict):
            if res.get("success") is False:
                raise RuntimeError(res.get("error", "get_objects failed"))
            if "objects" in res:
                return res["objects"]
            return []
        return res if isinstance(res, list) else []

    def get_object(self, doc_name: str, obj_name: str) -> dict[str, Any]:
        res = self._invoke("get_object", doc_name, obj_name)
        # New addon returns {"success": ..., "object": {...}};
        # old addon returns the dict directly.
        if isinstance(res, dict):
            if res.get("success") is False:
                raise RuntimeError(res.get("error", "get_object failed"))
            if "object" in res:
                return res["object"] or {}
        return res if isinstance(res, dict) else {}

    def list_documents(self) -> list[str]:
        res = self._invoke("list_documents")
        # New addon returns {"success": True, "documents": [...]};
        # old addon returns the list directly.
        if isinstance(res, dict) and "documents" in res:
            return res["documents"]
        return res if isinstance(res, list) else []
