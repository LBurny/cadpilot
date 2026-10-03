"""MCP side of get_addon_log: forwarding contract and old-addon tolerance.

The addon half (ring buffer, file rotation, instrumentation) is covered by
tests/test_dbglog.py plus a live FreeCAD smoke run; these tests pin what the
MCP layer sends and how it degrades against a stale Mod/ install.
"""

import json

from cadpilot.operations.core import get_addon_log_operation


def _text(resp) -> str:
    return "\n".join(c.text for c in resp)


class _OldAddonConnection:
    """A stale Mod/ install: no logging RPC at all."""


def test_forwards_every_parameter(fake_freecad):
    get_addon_log_operation(fake_freecad, level="WARNING", grep="tx", since_seq=12, limit=5)
    method, args, _kwargs = fake_freecad.calls[-1]
    assert method == "get_addon_log"
    assert args == ("WARNING", "tx", 12, 5)


def test_defaults_are_forwarded_as_none_and_zero(fake_freecad):
    get_addon_log_operation(fake_freecad)
    assert fake_freecad.calls[-1][1] == (None, None, 0, 100)


def test_returns_records_and_status(fake_freecad):
    payload = json.loads(_text(get_addon_log_operation(fake_freecad)))
    assert payload["records"][0]["message"] == "-> ping()"
    assert payload["records"][0]["request"] == "req#1"
    assert payload["status"]["setup_done"] is True


def test_old_addon_reports_clearly_instead_of_raising():
    out = _text(get_addon_log_operation(_OldAddonConnection()))
    assert "upgrade" in out.lower()


def test_connection_failure_is_surfaced(fake_freecad):
    fake_freecad.errors["get_addon_log"] = ConnectionError("refused")
    out = _text(get_addon_log_operation(fake_freecad))
    assert "Could not read the addon log" in out


def test_addon_error_is_surfaced(fake_freecad):
    fake_freecad.result_overrides["get_addon_log"] = {"success": False, "error": "boom"}
    assert "boom" in _text(get_addon_log_operation(fake_freecad))


def test_non_dict_response_is_surfaced(fake_freecad):
    fake_freecad.result_overrides["get_addon_log"] = None
    assert "Could not read the addon log" in _text(get_addon_log_operation(fake_freecad))


def test_empty_log_says_so_and_reports_status(fake_freecad):
    fake_freecad.result_overrides["get_addon_log"] = {
        "success": True,
        "records": [],
        "status": {"level": "INFO", "setup_done": True, "log_file": "x.log"},
    }
    out = _text(get_addon_log_operation(fake_freecad))
    assert "no records" in out.lower()
    assert "setup_done" in out


def test_forwarder_is_off_by_default(monkeypatch):
    from cadpilot import server

    monkeypatch.delenv("CADPILOT_FORWARD_ADDON_LOG", raising=False)
    monkeypatch.setattr(server, "_log_forwarder", None)
    server._maybe_start_log_forwarder()
    assert server._log_forwarder is None


def test_forwarder_opt_in_starts_a_daemon_thread(monkeypatch):
    from cadpilot import server

    monkeypatch.setenv("CADPILOT_FORWARD_ADDON_LOG", "1")
    monkeypatch.setattr(server, "_log_forwarder", None)
    server._maybe_start_log_forwarder()
    try:
        assert server._log_forwarder is not None
        # Daemon: a wedged addon must not keep the MCP process alive.
        assert server._log_forwarder.daemon is True
    finally:
        monkeypatch.setattr(server, "_log_forwarder", None)


def test_mcp_log_level_falls_back_on_a_typo():
    """logger.setLevel raises on unknown names, so the env value is validated."""
    from cadpilot.server import _resolve_mcp_log_level

    assert _resolve_mcp_log_level(None) == "INFO"
    assert _resolve_mcp_log_level("debug") == "DEBUG"
    assert _resolve_mcp_log_level("bogus") == "INFO"
