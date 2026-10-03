"""Fault diagnosis from the MCP side, with FreeCAD possibly absent.

Everything here runs in the MCP server process, so it still answers when
FreeCAD is frozen or was never started — which is exactly when
"connection refused" shows up. FreeCAD is never imported: the RPC endpoint,
the process, the addon installation and the addon's logs are all probed from
the outside.

Cross-platform by construction: user-data paths come from platform
conventions (Windows / macOS / Linux), process lookup prefers ``psutil`` and
degrades to ``tasklist`` or ``pgrep``, and every probe is best-effort — a
missing tool or an unreadable file is reported as unknown, never raised.
"""

from __future__ import annotations

import contextlib
import json
import locale
import os
import shutil
import socket
import subprocess
import sys
import time
import xmlrpc.client
from pathlib import Path

# FreeCAD's RPC server, as started by the addon.
DEFAULT_PORT = 9875

# Probing is interactive: never inherit the 150s modeling timeout.
PROBE_TIMEOUT = 5.0

# Subprocess probes must never be the thing that hangs a diagnosis.
_EXEC_TIMEOUT = 5.0

_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}

# What an actual bootstrap crash looks like in initgui_debug.log — the file
# also receives routine initgui lines, which must not be reported as a crash.
_CRASH_MARKERS = ("bootstrap crashed", "Traceback (most recent call last)")

# Where a bootstrap crash lands when __file__ is unavailable under FreeCAD's
# bare exec(): the working directory, i.e. the FreeCAD executable's folder.
_EXE_DIR_NAMES = ("freecad.exe", "freecadcmd.exe", "FreeCAD", "FreeCADCmd", "freecad", "freecadcmd")


def _run(cmd: list[str]) -> str | None:
    """Output of a probe command, or None when it is unavailable/failed.

    Captured as bytes and decoded by hand: ``text=True`` decodes with the
    locale codec, and a localized ``tasklist``/``netstat`` (e.g. a Chinese
    Windows console) then kills the pipe reader thread with a
    UnicodeDecodeError instead of returning the ASCII fields we parse.
    """
    if shutil.which(cmd[0]) is None:
        return None
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=_EXEC_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw = (proc.stdout or b"") + (proc.stderr or b"")
    for encoding in (locale.getpreferredencoding(False), "utf-8"):
        try:
            return raw.decode(encoding or "utf-8", errors="replace")
        except LookupError:
            continue
    return raw.decode("utf-8", errors="replace")


def _age_text(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}min ago"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h ago"
    return f"{seconds / 86400:.1f}d ago"


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


# --- user data / addon layout ------------------------------------------------


def config_root() -> Path:
    """FreeCAD's per-user config root (the directory holding ``Mod``)."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "FreeCAD"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "FreeCAD"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "FreeCAD"


def user_data_dirs() -> list[Path]:
    """Every user-data dir a FreeCAD build might actually be using.

    FreeCAD 1.x puts its files in a versioned subdirectory (``v1-1``) while
    older builds wrote straight into the config root, so an addon dropped in
    the wrong level is invisible to the running FreeCAD. Newest first, so the
    report leads with the build most likely in use.
    """
    root = config_root()
    cands: list[Path] = []
    with contextlib.suppress(OSError):
        cands += [p for p in root.iterdir() if p.is_dir() and p.name.startswith("v")]
    cands.sort(key=lambda p: _mtime(p) or 0, reverse=True)
    cands.append(root)
    # Legacy single-dir layout on Linux/macOS.
    if sys.platform != "win32":
        cands.append(Path.home() / ".FreeCAD")
    return [p for p in cands if p.is_dir()]


def addon_roots() -> list[Path]:
    """Candidate ``Mod/CADPilot`` directories, newest user dir first."""
    return [d / "Mod" / "CADPilot" for d in user_data_dirs()]


def addon_source_is_complete(path: Path) -> bool:
    return (path / "rpc_server" / "rpc_server.py").is_file()


def _resolve(path: Path) -> tuple[str, str]:
    """(kind, target) for an installed addon dir — symlink/junction or a copy."""
    try:
        real = Path(os.path.realpath(path))
    except OSError:
        return "unreadable", ""
    if real != path:
        return "link", str(real)
    return "copy", str(path)


def addon_status() -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for cand in addon_roots():
        if not cand.exists():
            continue
        kind, target = _resolve(cand)
        out.append(
            {
                "path": str(cand),
                "kind": kind,
                "target": target,
                "complete": addon_source_is_complete(cand),
                "mtime": _mtime(cand / "InitGui.py"),
            }
        )
    return out


# --- bootstrap crash log -----------------------------------------------------


def _exe_from_command(command: str) -> Path | None:
    """The executable from a shell open command, e.g. ``"C:\\...\\freecad.exe" "%1"``."""
    if not command:
        return None
    if command.lstrip().startswith('"'):
        end = command.find('"', 1)
        candidate = command[1:end] if end > 0 else ""
    else:
        candidate = command.split()[0] if command.split() else ""
    path = Path(candidate)
    return path if candidate and path.suffix.lower() == ".exe" and path.is_file() else None


def _windows_exe_dirs() -> list[Path]:
    """Install dirs derived from the .FCStd file association.

    A Windows FreeCAD install is normally not on PATH, and the user-data dir
    does not reveal where it lives — but the association registered by the
    installer does, and that is what points at the directory holding
    ``initgui_debug.log`` when a bootstrap crash could not resolve __file__.
    """
    import winreg

    dirs: list[Path] = []
    for hive in (winreg.HKEY_CLASSES_ROOT, winreg.HKEY_CURRENT_USER):
        exe: Path | None = None
        with contextlib.suppress(OSError, ValueError):
            with winreg.OpenKey(hive, r"Software\Classes\.FCStd") as key:
                progid = str(winreg.QueryValueEx(key, "")[0])
            path = rf"Software\Classes\{progid}\shell\open\command"
            with winreg.OpenKey(hive, path) as key:
                exe = _exe_from_command(str(winreg.QueryValueEx(key, "")[0]))
        if exe is not None:
            dirs.append(exe.parent)
    return dirs


def freecad_exe_dirs() -> list[Path]:
    """Directories a FreeCAD executable might have been launched from."""
    dirs: list[Path] = [Path(os.getcwd())]
    for exe in _EXE_DIR_NAMES:
        found = shutil.which(exe)
        if found:
            dirs.append(Path(found).parent)
    if sys.platform == "win32":
        dirs += _windows_exe_dirs()
    elif sys.platform == "darwin":
        dirs.append(Path("/Applications/FreeCAD.app/Contents/MacOS"))
    else:
        dirs += [Path("/usr/lib/freecad/bin"), Path("/usr/lib/freecad-daily/bin")]
    out: list[Path] = []
    for d in dirs:
        if d not in out:
            out.append(d)
    return out


def _crash_tail(text: str) -> list[str]:
    """The last crash block in a bootstrap log, else the last lines.

    A bootstrap crash is appended, so the traceback is normally at the end —
    but the same file also receives routine initgui lines from builds whose
    logging framework failed to load, and the tail must not mislabel those as
    a crash.
    """
    lines = text.strip().splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if any(marker in lines[i] for marker in _CRASH_MARKERS):
            return lines[i : i + 12]
    return lines[-6:]


def bootstrap_logs(extra_dirs: list[Path] | None = None) -> list[dict[str, object]]:
    """``initgui_debug.log`` files, flagged as an actual crash or not.

    This is the only evidence of a failed addon load. InitGui.py writes one
    when its bootstrap dies before the logging framework is up, and under
    FreeCAD's bare ``exec()`` it may land next to the FreeCAD executable
    instead of in the addon dir, so both are searched (``extra_dirs`` lets the
    caller add the directory a *running* FreeCAD was started from).
    """
    seen: dict[str, dict[str, object]] = {}
    cands = [d / "initgui_debug.log" for d in addon_roots()]
    cands += [d / "initgui_debug.log" for d in freecad_exe_dirs()]
    cands += [d / "initgui_debug.log" for d in extra_dirs or []]
    for path in cands:
        key = str(path)
        if key in seen:
            continue
        if not path.is_file() or not path.stat().st_size:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        seen[key] = {
            "path": key,
            "mtime": _mtime(path),
            "crash": any(marker in text for marker in _CRASH_MARKERS),
            "tail": _crash_tail(text),
        }
    # Crashes first: they are the reason this section exists.
    return sorted(seen.values(), key=lambda e: (not e["crash"], -(e["mtime"] or 0)))


# --- addon logging / settings ------------------------------------------------


def addon_log_status() -> list[dict[str, object]]:
    """``<user dir>/CADPilot/logs/cadpilot.log`` freshness, newest first."""
    out: list[dict[str, object]] = []
    now = time.time()
    for d in user_data_dirs():
        log = d / "CADPilot" / "logs" / "cadpilot.log"
        if not log.is_file():
            continue
        mt = _mtime(log)
        out.append(
            {
                "path": str(log),
                "mtime": mt,
                "age_s": None if mt is None else now - mt,
                "size": log.stat().st_size,
            }
        )
    out.sort(key=lambda r: r["mtime"] or 0, reverse=True)
    return out


def settings_status() -> list[dict[str, object]]:
    """``cadpilot_settings.json`` per user dir: auto-start / remote flags."""
    out: list[dict[str, object]] = []
    for d in user_data_dirs():
        f = d / "cadpilot_settings.json"
        if not f.is_file():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            out.append({"path": str(f), "error": f"{type(e).__name__}: {e}"})
            continue
        keys = (
            "auto_start_rpc",
            "allow_remote_connections",
            "allowed_ips",
            "rpc_port",
        )
        out.append({"path": str(f), **{k: data.get(k) for k in keys if k in data}})
    return out


# --- process / port probes ---------------------------------------------------


def freecad_processes() -> tuple[str, list[dict[str, object]]]:
    """(method, processes) — psutil when present, else a platform command."""
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        pass  # fall through to the platform command
    else:
        procs: list[dict[str, object]] = []
        for p in psutil.process_iter(["pid", "name", "create_time", "exe"]):
            name = (p.info.get("name") or "").lower()
            if name.startswith("freecad"):
                created = p.info.get("create_time")
                procs.append(
                    {
                        "pid": p.info.get("pid"),
                        "name": p.info.get("name"),
                        "started": None if not created else _age_text(time.time() - created),
                        "exe": p.info.get("exe"),
                    }
                )
        return "psutil", procs

    if sys.platform == "win32":
        text = _run(["tasklist", "/FI", "IMAGENAME eq FreeCAD.exe"])
        found = [ln for ln in (text or "").splitlines() if "freecad" in ln.lower()]
        return "tasklist", [{"line": ln.strip()} for ln in found]
    text = _run(["pgrep", "-fl", "freecad"])
    found = [ln for ln in (text or "").splitlines() if ln.strip()]
    return "pgrep", [{"line": ln.strip()} for ln in found]


def port_listeners(port: int) -> tuple[str, list[str]]:
    """Who is listening on *port*, best-effort.

    Distinguishes the two failures that look identical from the client side: a
    closed port (RPC server not started) from an open port that never answers
    (FreeCAD's GUI thread is wedged — restart FreeCAD, the addon log is still
    readable).
    """
    if sys.platform == "win32":
        text = _run(["netstat", "-ano", "-p", "TCP"])
        if text is None:
            return "unavailable", []
        return "netstat", [ln.strip() for ln in text.splitlines() if f":{port}" in ln]
    cmd = ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"]
    text = _run(cmd)
    if text is None:
        text = _run(["ss", "-ltnp"])  # Linux fallback
        if text is None:
            return "unavailable", []
        return "ss", [ln.strip() for ln in text.splitlines() if f":{port}" in ln]
    return "lsof", [ln.strip() for ln in text.splitlines()[1:] if ln.strip()]


class _ShortTimeoutTransport(xmlrpc.client.Transport):
    """A probe must fail fast — the modeling client's 150s timeout is useless here."""

    def __init__(self, timeout: float = PROBE_TIMEOUT, **kwargs):
        super().__init__(**kwargs)
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


def probe_rpc(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> dict[str, object]:
    """Ping the addon's XML-RPC endpoint on a short timeout."""
    from .freecad_client import _resolve_connect_host

    target = _resolve_connect_host(host)
    uri = f"http://{target}:{port}"
    started = time.time()
    proxy = xmlrpc.client.ServerProxy(
        uri, allow_none=True, transport=_ShortTimeoutTransport(timeout)
    )
    try:
        result = proxy.ping()
    except Exception as e:
        return {
            "reachable": False,
            "uri": uri,
            "error": f"{type(e).__name__}: {e}",
            "elapsed_ms": round((time.time() - started) * 1000),
        }
    return {
        "reachable": bool(result),
        "uri": uri,
        "elapsed_ms": round((time.time() - started) * 1000),
    }


def tcp_open(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    """Raw socket connect: is anything at all accepting on the port?"""
    from .freecad_client import _resolve_connect_host

    try:
        with socket.create_connection((_resolve_connect_host(host), port), timeout):
            return True
    except OSError:
        return False


# --- the generic flow (platform independent) ---------------------------------

CHECKLIST = """\
Diagnostic order that applies on Windows, macOS and Linux alike:
1. FreeCAD running?     A closed port with no process means nothing to talk to.
2. RPC server started?  In FreeCAD: CADPilot toolbar -> "RPC Server" (or set
                        auto_start_rpc). Auto-start only happens at startup.
3. Addon installed?     <user data dir>/Mod/CADPilot must exist for the SAME
                        FreeCAD version that is running (v1-1, v0-21, ...).
4. Did it load?         Read initgui_debug.log (addon dir, or next to the
                        FreeCAD executable) — a bootstrap crash lands there and
                        the addon then stays invisible, with no workbench, no
                        RPC server and a stale addon log.
5. Changed anything?    Addon files, MCP client config and the tool list are
                        only picked up after a restart: restart FreeCAD, then
                        the MCP client (it builds tools/list at startup).
"""


def _verdict(
    rpc: dict,
    listeners: list[str],
    procs: list[dict],
    logs: list[dict],
    crash_logs: list[dict] | None = None,
) -> str:
    if rpc.get("reachable"):
        return "FreeCAD is reachable — the RPC server answered."
    if listeners:
        return (
            "The port is LISTENING but ping did not answer: FreeCAD is up with "
            "the RPC server open, and its GUI thread is busy or wedged (a modal "
            "dialog, a long recompute, or a deadlock). The addon log stays "
            "readable via get_addon_log; restart FreeCAD if it does not clear."
        )
    crashes = [e for e in crash_logs or [] if e.get("crash")]
    if crashes:
        return (
            "FreeCAD is running but the addon DID NOT LOAD: "
            f"{crashes[0]['path']} records a bootstrap crash (see below). Fix the "
            "crash, then restart FreeCAD — the addon, its workbench and the RPC "
            "server all come up together at startup."
        )
    if procs:
        return (
            "FreeCAD is running but nothing is listening on the port: the addon is "
            "not loaded, or its RPC server was not started (toolbar toggle / "
            "auto_start_rpc only applies at startup)."
        )
    if logs:
        return (
            "Nothing is listening and no FreeCAD process answered the lookup: "
            "FreeCAD is not running (or runs in a session this probe cannot see)."
        )
    return (
        "FreeCAD does not appear to be running at all — start it, and make sure "
        "the addon is installed for the version you start."
    )


def diagnose(host: str, port: int = DEFAULT_PORT, timeout: float = PROBE_TIMEOUT) -> dict:
    """Collect every probe into one structured report (never raises)."""
    is_local = host.strip().lower() in _LOCAL_HOSTS
    report: dict[str, object] = {
        "platform": sys.platform,
        "host": host,
        "port": port,
        "local": is_local,
    }

    try:
        rpc = probe_rpc(host, port, timeout)
    except Exception as e:
        rpc = {"reachable": False, "error": f"{type(e).__name__}: {e}"}
    opened = False if rpc.get("reachable") else tcp_open(host, port, timeout)
    report["rpc"] = rpc
    report["tcp_open"] = opened

    if is_local:
        method, procs = freecad_processes()
        report["process_method"] = method
        report["processes"] = procs
        lmethod, listeners = port_listeners(port)
        report["listener_method"] = lmethod
    else:
        listeners = []
        procs = []
        report["processes"] = []
        report["remote_note"] = (
            "FreeCAD runs on another machine: local addon, user-data and log "
            "checks were skipped, only the network probe applies."
        )
    report["listeners"] = listeners

    if is_local:
        report["addon"] = addon_status()
        report["user_dirs"] = [str(d) for d in user_data_dirs()]
        # Where the *running* FreeCAD was started from is the likeliest home of
        # a bootstrap crash log that could not resolve __file__.
        proc_dirs = [Path(p["exe"]).parent for p in procs if p.get("exe")]
        report["bootstrap_logs"] = bootstrap_logs(proc_dirs)
        report["addon_logs"] = addon_log_status()
        report["settings"] = settings_status()

    report["verdict"] = _verdict(
        rpc, listeners, procs, report.get("addon_logs") or [], report.get("bootstrap_logs")
    )
    return report


def format_report(report: dict) -> str:
    """Human/model-readable rendering of :func:`diagnose`."""
    lines: list[str] = []
    stamp = time.strftime("%Y-%m-%d %H:%M")
    lines.append(f"CADPilot diagnosis — {stamp} ({report.get('platform')})")

    rpc = report.get("rpc") or {}
    mark = "reachable" if rpc.get("reachable") else "UNREACHABLE"
    detail = f"{rpc.get('elapsed_ms')}ms" if rpc.get("reachable") else rpc.get("error", "")
    lines.append(f"RPC {rpc.get('uri')} : {mark} {detail}".rstrip())

    if report.get("tcp_open"):
        lines.append("TCP port : open (something accepts connections)")
    elif not rpc.get("reachable"):
        lines.append("TCP port : closed")

    listeners = report.get("listeners") or []
    if listeners:
        lines.append(f"Listening ({report.get('listener_method')}):")
        lines += [f"    {ln}" for ln in listeners[:4]]

    procs = report.get("processes") or []
    if procs:
        lines.append(f"FreeCAD process ({report.get('process_method')}):")
        for p in procs[:4]:
            if "line" in p:
                lines.append(f"    {p['line']}")
            else:
                lines.append(f"    pid {p.get('pid')} {p.get('name')} started {p.get('started')}")
    elif report.get("local"):
        lines.append("FreeCAD process : none found")

    if report.get("remote_note"):
        lines.append(report["remote_note"])

    dirs = report.get("user_dirs") or []
    if dirs:
        lines.append("FreeCAD user dirs:")
        lines += [f"    {d}" for d in dirs]

    for entry in report.get("addon") or []:
        kind = entry.get("kind")
        loc = entry.get("target") if kind == "link" else entry.get("path")
        state = "ok" if entry.get("complete") else "INCOMPLETE (rpc_server missing)"
        lines.append(f"Addon install : {entry.get('path')}")
        lines.append(f"    {kind} -> {loc} [{state}]")

    for entry in report.get("bootstrap_logs") or []:
        age = _age_text(None if entry.get("mtime") is None else time.time() - entry["mtime"])
        if entry.get("crash"):
            # A reachable RPC means the addon is loaded now, so the crash is
            # from an earlier start and must not read as the current failure.
            mark = "BOOTSTRAP CRASH" + ("" if not rpc.get("reachable") else " (earlier start)")
            lines.append(f"{mark} ({age}): {entry.get('path')}")
            lines += [f"    {ln}" for ln in entry.get("tail") or []]
        else:
            # Routine initgui lines from a build whose logger was unavailable:
            # worth naming, but calling this a crash would be a false lead.
            lines.append(f"Bootstrap log, no crash ({age}): {entry.get('path')}")

    for entry in report.get("addon_logs") or []:
        lines.append(
            f"Addon log : {entry.get('path')} "
            f"(last write {_age_text(entry.get('age_s'))}, {entry.get('size')}B)"
        )

    for entry in report.get("settings") or []:
        flags = {k: v for k, v in entry.items() if k != "path"}
        lines.append(f"Settings : {entry.get('path')} {flags}")

    lines.append(f"\nVerdict: {report.get('verdict')}")
    lines.append("\n" + CHECKLIST)
    return "\n".join(lines)
