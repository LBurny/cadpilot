"""Tests for FreeCADConnection reconnect and screenshot-fallback logic.

The XML-RPC proxy is faked by monkeypatching FreeCADConnection._make_proxy,
so no real server is needed.
"""

import socket
import xmlrpc.client

import pytest

from cadpilot.freecad_client import FreeCADConnection


class FlakyProxy:
    """Fails the first `fail_times` calls with a recoverable error."""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times

    def create_document(self, name):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionResetError(10054, "connection reset")
        return {"success": True, "document_name": name}


def _make_conn(monkeypatch, proxy_factory):
    """Build a FreeCADConnection whose _make_proxy uses proxy_factory."""
    made = []

    def fake_make(self, timeout):
        proxy = proxy_factory(len(made))
        made.append(proxy)
        return proxy

    monkeypatch.setattr(FreeCADConnection, "_make_proxy", fake_make)
    conn = FreeCADConnection()
    return conn, made


def test_reconnects_once_on_connection_reset(monkeypatch):
    conn, made = _make_conn(monkeypatch, lambda n: FlakyProxy(fail_times=1 if n == 0 else 0))
    res = conn.create_document("Doc")
    assert res == {"success": True, "document_name": "Doc"}
    assert len(made) == 2  # original proxy + one rebuild


def test_reconnect_failure_propagates(monkeypatch):
    conn, made = _make_conn(monkeypatch, lambda n: FlakyProxy(fail_times=99))
    with pytest.raises(ConnectionResetError):
        conn.create_document("Doc")
    assert len(made) == 2  # retried exactly once


def test_socket_timeout_is_not_retried(monkeypatch):
    class TimeoutProxy:
        def create_document(self, name):
            raise TimeoutError("timed out")

    conn, made = _make_conn(monkeypatch, lambda n: TimeoutProxy())
    with pytest.raises(socket.timeout):
        conn.create_document("Doc")
    assert len(made) == 1  # no rebuild: the op may still be executing server-side


def test_xmlrpc_fault_is_not_retried(monkeypatch):
    class FaultProxy:
        def create_document(self, name):
            raise xmlrpc.client.Fault(1, "server error")

    conn, made = _make_conn(monkeypatch, lambda n: FaultProxy())
    with pytest.raises(xmlrpc.client.Fault):
        conn.create_document("Doc")
    assert len(made) == 1


class LegacyAddonProxy:
    """Simulates an old addon: create_object takes (doc_name, obj_data) only."""

    def __init__(self):
        self.screenshot_calls = 0

    def create_object(self, doc_name, obj_data, *extra):
        if extra:
            raise xmlrpc.client.Fault(
                1,
                "<class 'TypeError'>:create_object() takes 3 positional arguments but 4 were given",
            )
        return {"success": True, "object_name": obj_data.get("Name")}

    def get_active_screenshot(
        self, view_name="Isometric", width=None, height=None, focus_object=None
    ):
        self.screenshot_calls += 1
        return "ZmFrZQ=="


def test_screenshot_param_falls_back_to_legacy_two_call_path(monkeypatch):
    proxy = LegacyAddonProxy()
    conn, _made = _make_conn(monkeypatch, lambda n: proxy)
    res = conn.create_object("Doc", {"Name": "Box"}, screenshot={"view_name": "Isometric"})
    assert res["success"] is True
    assert res["screenshot"] == "ZmFrZQ=="
    assert proxy.screenshot_calls == 1


def test_no_screenshot_param_means_single_call(monkeypatch):
    proxy = LegacyAddonProxy()
    conn, _ = _make_conn(monkeypatch, lambda n: proxy)
    res = conn.create_object("Doc", {"Name": "Box"})
    assert "screenshot" not in res
    assert proxy.screenshot_calls == 0


# --- get_objects / get_object failure propagation (document-not-found) -------


class DocQueryProxy:
    """Mirrors the new addon's envelope responses for get_objects/get_object."""

    def __init__(self, res):
        self.res = res

    def get_objects(self, doc_name):
        return self.res

    def get_object(self, doc_name, obj_name):
        return self.res


def test_get_objects_raises_on_failure_envelope(monkeypatch):
    proxy = DocQueryProxy({"success": False, "error": "Document 'X' not found.", "objects": []})
    conn, _ = _make_conn(monkeypatch, lambda n: proxy)
    with pytest.raises(RuntimeError, match="not found"):
        conn.get_objects("X")


def test_get_object_raises_on_failure_envelope(monkeypatch):
    proxy = DocQueryProxy({"success": False, "error": "Document 'X' not found.", "object": None})
    conn, _ = _make_conn(monkeypatch, lambda n: proxy)
    with pytest.raises(RuntimeError, match="not found"):
        conn.get_object("X", "Box")


def test_get_objects_success_envelope_still_works(monkeypatch):
    proxy = DocQueryProxy({"success": True, "objects": [{"Name": "Box"}]})
    conn, _ = _make_conn(monkeypatch, lambda n: proxy)
    assert conn.get_objects("Doc") == [{"Name": "Box"}]


def test_get_objects_legacy_list_response_still_works(monkeypatch):
    proxy = DocQueryProxy([{"Name": "Box"}])
    conn, _ = _make_conn(monkeypatch, lambda n: proxy)
    assert conn.get_objects("Doc") == [{"Name": "Box"}]
