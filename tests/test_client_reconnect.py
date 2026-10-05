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


def test_localhost_is_pinned_to_ipv4(monkeypatch):
    """`localhost` resolves to ::1 first, which the addon never binds.

    The failed IPv6 connect cost ~2s per call on Windows; the loopback name is
    therefore rewritten at connection time.
    """
    monkeypatch.setattr(FreeCADConnection, "_make_proxy", lambda self, timeout: None)
    assert FreeCADConnection()._uri == "http://127.0.0.1:9875"
    assert FreeCADConnection(host="LOCALHOST")._uri == "http://127.0.0.1:9875"


def test_remote_hosts_are_passed_through(monkeypatch):
    monkeypatch.setattr(FreeCADConnection, "_make_proxy", lambda self, timeout: None)
    assert FreeCADConnection(host="192.168.1.50")._uri == "http://192.168.1.50:9875"
    assert (
        FreeCADConnection(host="build-box.local", port=9999)._uri == "http://build-box.local:9999"
    )


# --- connection-refused grace window -----------------------------------------


class RefusingProxy:
    """Fails the first `fail_times` calls with ConnectionRefusedError.

    A refusal happens on the TCP connect, before anything is sent, so unlike
    a reset mid-request it may be retried repeatedly within the grace window.
    """

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.attempts = 0

    def create_document(self, name):
        self.attempts += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionRefusedError(10061, "refused")
        return {"success": True, "document_name": name}


def test_connection_refused_retries_within_grace(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("cadpilot.freecad_client.time.sleep", sleeps.append)
    proxy = RefusingProxy(fail_times=3)
    conn, _made = _make_conn(monkeypatch, lambda n: proxy)
    res = conn.create_document("Doc")
    assert res == {"success": True, "document_name": "Doc"}
    assert proxy.attempts > 1  # retried instead of failing on first refusal
    assert sleeps  # the retries waited out the outage


def test_connection_refused_gives_up_after_grace(monkeypatch):
    monkeypatch.setattr("cadpilot.freecad_client.time.sleep", lambda _s: None)
    proxy = RefusingProxy(fail_times=10**9)
    conn, _made = _make_conn(monkeypatch, lambda n: proxy)
    conn._connect_grace = 0.0
    with pytest.raises(ConnectionRefusedError, match="refused connections"):
        conn.create_document("Doc")


def test_zero_grace_still_names_the_check_to_run(monkeypatch):
    monkeypatch.setattr("cadpilot.freecad_client.time.sleep", lambda _s: None)
    conn, _made = _make_conn(monkeypatch, lambda n: RefusingProxy(fail_times=1))
    conn._connect_grace = 0.0
    with pytest.raises(ConnectionRefusedError, match="RPC server is started"):
        conn.create_document("Doc")


def test_connect_grace_env_override(monkeypatch):
    monkeypatch.setattr(FreeCADConnection, "_make_proxy", lambda self, timeout: None)
    monkeypatch.setenv("CADPILOT_CONNECT_GRACE", "2.5")
    assert FreeCADConnection()._connect_grace == 2.5
    monkeypatch.setenv("CADPILOT_CONNECT_GRACE", "bogus")
    assert FreeCADConnection()._connect_grace == 10.0  # invalid value → default
