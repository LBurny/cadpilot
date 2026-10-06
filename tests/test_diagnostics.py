"""Diagnostics — the fault-finding tool must work with FreeCAD down.

That is the whole point of the module: it runs in the MCP server process and
only looks at FreeCAD from the outside, so its probes have to stay
best-effort and never raise. The tests cover the layout/parse helpers, a live
loopback RPC endpoint, a closed port, and the rendering of a full report.
"""

import os
import socket
import sys
import threading
import time
import xmlrpc.server
from pathlib import Path

import pytest

from cadpilot import diagnostics as diag


def _userdir(tmp_path, version="v1-1", monkeypatch=None) -> Path:
    """A fake FreeCAD config root containing one versioned user dir."""
    root = tmp_path / "FreeCAD"
    (root / version / "Mod").mkdir(parents=True)
    if monkeypatch is not None:
        monkeypatch.setattr(diag, "config_root", lambda: root)
    return root / version


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --- platform layout ---------------------------------------------------------


def test_config_root_follows_the_platform_convention(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    root = diag.config_root()
    assert root.is_absolute()
    assert root.name == "FreeCAD"
    if sys.platform == "darwin":
        assert "Application Support" in str(root)  # macOS ignores XDG
    else:
        assert root.parent == tmp_path


def test_user_data_dirs_probe_newest_version_first(tmp_path, monkeypatch):
    root = tmp_path / "FreeCAD"
    for version, stamp in (("v1-0", 1_000), ("v1-1", 2_000)):
        (root / version / "Mod").mkdir(parents=True)
        os.utime(root / version, (stamp, stamp))
    monkeypatch.setattr(diag, "config_root", lambda: root)

    dirs = diag.user_data_dirs()
    assert dirs[0] == root / "v1-1", "the build most likely in use must lead"
    assert root in dirs, "the unversioned (pre-1.0) layout is still probed"


def test_addon_status_flags_an_incomplete_install(tmp_path, monkeypatch):
    userdir = _userdir(tmp_path, monkeypatch=monkeypatch)
    good = userdir / "Mod" / "CADPilot"
    (good / "rpc_server").mkdir(parents=True)
    (good / "rpc_server" / "rpc_server.py").write_text("# addon\n", encoding="utf-8")
    (good / "InitGui.py").write_text("# gui\n", encoding="utf-8")
    (userdir / "Mod" / "CADPilot_Old" / "rpc_server").mkdir(parents=True)  # not CADPilot

    entries = diag.addon_status()
    assert len(entries) == 1, "only Mod/CADPilot counts, not every sibling"
    assert entries[0]["complete"] is True
    assert entries[0]["kind"] == "copy"


def test_addon_status_reports_a_linked_install(tmp_path, monkeypatch):
    userdir = _userdir(tmp_path, monkeypatch=monkeypatch)
    source = tmp_path / "src-addon"
    (source / "rpc_server").mkdir(parents=True)
    (source / "rpc_server" / "rpc_server.py").write_text("# addon\n", encoding="utf-8")
    link = userdir / "Mod" / "CADPilot"
    try:
        link.symlink_to(source, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable (Windows without developer mode)")

    entry = diag.addon_status()[0]
    assert entry["kind"] == "link"
    assert Path(entry["target"]).resolve() == source.resolve()


# --- the failure evidence ----------------------------------------------------


def test_bootstrap_crash_log_is_surfaced_with_its_traceback(tmp_path, monkeypatch):
    userdir = _userdir(tmp_path, monkeypatch=monkeypatch)
    addon = userdir / "Mod" / "CADPilot"
    addon.mkdir(parents=True)
    (addon / "initgui_debug.log").write_text(
        "=== 00:11:15 bootstrap crashed\n"
        "Traceback (most recent call last):\n"
        "NameError: name 'contextlib' is not defined\n",
        encoding="utf-8",
    )

    logs = diag.bootstrap_logs()
    mine = [e for e in logs if e["path"] == str(addon / "initgui_debug.log")]
    assert mine, "a crash log in the addon dir must be reported"
    assert mine[0]["crash"] is True
    assert any("NameError" in line for line in mine[0]["tail"])


def test_routine_bootstrap_output_is_not_reported_as_a_crash(tmp_path, monkeypatch):
    """initgui_debug.log also receives watchdog lines; a false crash report is
    worse than none, because it sends the reader after the wrong cause."""
    userdir = _userdir(tmp_path, monkeypatch=monkeypatch)
    addon = userdir / "Mod" / "CADPilot"
    addon.mkdir(parents=True)
    (addon / "initgui_debug.log").write_text(
        "=== 19:24:51 ensure_bar(watchdog) created=True actions=5\n",
        encoding="utf-8",
    )

    entry = next(e for e in diag.bootstrap_logs() if e["path"] == str(addon / "initgui_debug.log"))
    assert entry["crash"] is False
    text = diag.format_report({"platform": "linux", "rpc": {}, "bootstrap_logs": [entry]})
    assert "BOOTSTRAP CRASH" not in text
    assert "no crash" in text


def test_addon_log_age_marks_a_stale_log(tmp_path, monkeypatch):
    userdir = _userdir(tmp_path, monkeypatch=monkeypatch)
    log = userdir / "CADPilot" / "logs" / "cadpilot.log"
    log.parent.mkdir(parents=True)
    log.write_text("old\n", encoding="utf-8")
    old = time.time() - 7200
    os.utime(log, (old, old))

    entry = diag.addon_log_status()[0]
    assert entry["path"] == str(log)
    assert entry["age_s"] > 3600, "a log untouched for 2h is what 'never loaded' looks like"
    assert "h ago" in diag._age_text(entry["age_s"])


def test_settings_status_reads_the_rpc_flags(tmp_path, monkeypatch):
    userdir = _userdir(tmp_path, monkeypatch=monkeypatch)
    (userdir / "cadpilot_settings.json").write_text(
        '{"auto_start_rpc": true, "allowed_ips": ["127.0.0.1"], "unrelated": 1}',
        encoding="utf-8",
    )

    entry = diag.settings_status()[0]
    assert entry["auto_start_rpc"] is True
    assert entry["allowed_ips"] == ["127.0.0.1"]
    assert "unrelated" not in entry


def test_unparseable_settings_do_not_raise(tmp_path, monkeypatch):
    userdir = _userdir(tmp_path, monkeypatch=monkeypatch)
    (userdir / "cadpilot_settings.json").write_text("{not json", encoding="utf-8")
    assert "error" in diag.settings_status()[0]


# --- network probes ----------------------------------------------------------


def test_probe_rpc_reaches_a_live_endpoint():
    server = xmlrpc.server.SimpleXMLRPCServer(("127.0.0.1", 0), allow_none=True, logRequests=False)
    server.register_function(lambda: True, "ping")
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        res = diag.probe_rpc("127.0.0.1", port, timeout=5)
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert res["reachable"] is True
    assert res["uri"].endswith(str(port))


def test_probe_rpc_and_tcp_probe_report_a_closed_port():
    port = _closed_port()
    res = diag.probe_rpc("127.0.0.1", port, timeout=1)
    assert res["reachable"] is False
    assert res["error"]
    assert diag.tcp_open("127.0.0.1", port, timeout=1) is False


def test_probe_gui_state_reads_the_back_pressure_reason():
    """The recovery dialog FreeCAD opens after an unclean shutdown blocks every
    document call while ping still answers (it is not GUI-dispatched), so the
    only way to tell that from a wedged thread is to ask the addon."""
    server = xmlrpc.server.SimpleXMLRPCServer(("127.0.0.1", 0), allow_none=True, logRequests=False)
    server.register_function(
        lambda: {
            "success": True,
            "defer_reason": "modal",
            "defer_label": "a modal dialog is open",
            "deferred_s": 12.5,
            "processing": False,
            "processing_s": 0.0,
            "queue_depth": 2,
        },
        "get_gui_state",
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        state = diag.probe_gui_state("127.0.0.1", port, timeout=5)
        note = diag.gui_state_note(state)
        verdict = diag._verdict({"reachable": True}, [], [], [], None, state)
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert state["available"] is True
    assert state["defer_reason"] == "modal"
    assert "MODAL DIALOG" in note
    assert "dismiss_blocking_dialog" in note, "the programmatic way out must come first"
    assert "Cancel" in note and "Start recovery" in note, "the note must name the resolution"
    # The prose is ENGLISH like every other CADPilot message; only the literal
    # on-screen button labels appear in their localized form, in parentheses.
    cjk = [ch for ch in note if "\u4e00" <= ch <= "\u9fff"]
    inside = []
    depth = 0
    for ch in note:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth and "\u4e00" <= ch <= "\u9fff":
            inside.append(ch)
    assert cjk and len(inside) == len(cjk), f"localized labels must stay parenthesized: {note}"
    assert "MODAL DIALOG" in verdict
    # An addon that predates the method is reported, not treated as a fault.
    assert diag.gui_state_note({"available": False, "error": "no get_gui_state"}) == "", (
        "an old addon must not be blamed"
    )
    assert diag._verdict({"reachable": True}, [], [], []) == (
        "FreeCAD is reachable — the RPC server answered."
    )


# --- verdict + report --------------------------------------------------------


def test_verdict_separates_wedged_freecad_from_a_missing_server():
    unreachable = {"reachable": False}
    wedged = diag._verdict(
        unreachable, ["TCP 0.0.0.0:9875 LISTENING"], [{"pid": 1}], [{"path": "L"}]
    )
    assert "LISTENING" in wedged and "GUI thread" in wedged

    not_started = diag._verdict(unreachable, [], [{"pid": 1}], [{"path": "L"}])
    assert "not loaded" in not_started or "was not started" in not_started

    down = diag._verdict(unreachable, [], [], [])
    assert "not appear to be running" in down

    assert "reachable" in diag._verdict({"reachable": True}, [], [], [])


def test_verdict_points_at_the_crash_log_when_the_addon_did_not_load():
    crash = [{"path": "/mod/CADPilot/initgui_debug.log", "crash": True}]
    verdict = diag._verdict({"reachable": False}, [], [{"pid": 7}], [{"path": "L"}], crash)
    assert "DID NOT LOAD" in verdict
    assert "initgui_debug.log" in verdict

    # A listening port outranks crash evidence: the addon is loaded as of now.
    wedged = diag._verdict({"reachable": False}, ["LISTEN"], [{"pid": 7}], [], crash)
    assert "LISTENING" in wedged


def test_diagnose_reports_a_remote_host_without_local_checks():
    report = diag.diagnose("10.255.255.1", port=_closed_port(), timeout=0.3)
    assert report["local"] is False
    assert "remote_note" in report
    assert "addon" not in report, "local addon paths are meaningless for a remote FreeCAD"


def test_diagnose_never_raises_and_renders_a_report(tmp_path, monkeypatch):
    root = tmp_path / "FreeCAD"
    (root / "v1-1" / "Mod" / "CADPilot").mkdir(parents=True)
    monkeypatch.setattr(diag, "config_root", lambda: root)

    report = diag.diagnose("127.0.0.1", port=_closed_port(), timeout=0.5)
    assert set(report) >= {"platform", "rpc", "verdict", "user_dirs", "addon_logs"}
    text = diag.format_report(report)
    assert "CADPilot diagnosis" in text
    assert "Verdict:" in text
    assert "restart" in text.lower(), "the restart rule is part of the generic flow"


def test_format_report_renders_every_section():
    report = {
        "platform": "linux",
        "local": True,
        "rpc": {"reachable": False, "uri": "http://127.0.0.1:9875", "error": "ConnectionRefused"},
        "tcp_open": False,
        "listener_method": "ss",
        "listeners": ["LISTEN 0 5 0.0.0.0:9875"],
        "process_method": "psutil",
        "processes": [{"pid": 42, "name": "FreeCAD", "started": "3min ago"}],
        "user_dirs": ["/home/u/.local/share/FreeCAD/v1-1"],
        "addon": [
            {"path": "/mod/CADPilot", "kind": "link", "target": "/src/addon", "complete": True}
        ],
        "bootstrap_logs": [
            {
                "path": "/mod/CADPilot/initgui_debug.log",
                "mtime": time.time(),
                "crash": True,
                "tail": ["boom"],
            }
        ],
        "addon_logs": [{"path": "/log/cadpilot.log", "age_s": 30, "size": 12}],
        "settings": [{"path": "/s.json", "auto_start_rpc": True}],
        "verdict": "FreeCAD is not running.",
    }
    text = diag.format_report(report)
    for needle in (
        "UNREACHABLE",
        "TCP port : closed",
        "LISTEN 0 5",
        "pid 42",
        "/mod/CADPilot",
        "boom",
        "/log/cadpilot.log",
        "auto_start_rpc",
        "FreeCAD is not running.",
    ):
        assert needle in text, needle


def test_diagnose_help_topic_documents_the_flow():
    from cadpilot.operations import operation_help_operation

    text = " ".join(c.text for c in operation_help_operation("diagnose") if hasattr(c, "text"))
    assert "initgui_debug.log" in text
    assert "v1-1" in text and "restart" in text.lower()
