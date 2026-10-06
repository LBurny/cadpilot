"""CADPilot addon debug logging: ring buffer + rotating file + Report View.

The addon's only logging infrastructure. One pipeline, three consumers:

* the in-memory ring buffer behind the ``get_addon_log`` RPC/MCP tool, so an
  AI client or the user can pull recent diagnostics without touching a file;
* a rotating file under FreeCAD's user data dir, for anything that must
  outlive the process (crashes, restarts);
* FreeCAD's Report View, for WARNING and above — the one place a human looks
  by default.

FreeCAD is imported defensively: this module must stay importable from
``tests/`` without FreeCAD, and must never be the reason logging fails.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import threading
from collections import deque
from datetime import datetime
from typing import Any

try:  # guarded: tests import this module without FreeCAD
    import FreeCAD
except ImportError:  # pragma: no cover - only outside FreeCAD
    FreeCAD = None

ROOT_LOGGER = "CADPilot"
LOG_FILENAME = "cadpilot.log"
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
DEFAULT_LEVEL = "INFO"
DEFAULT_RING_SIZE = 2000
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_BACKUPS = 3
MAX_FIELD_CHARS = 300
# Tracebacks and `extra` payloads get much more room: they only appear when
# something went wrong, and a clipped traceback is barely worth reading.
MAX_DETAIL_CHARS = 4000
TRUNCATE_SUFFIX = "..."

_FILE_FORMAT = "%(asctime)s %(levelname)-7s %(threadName)-12s %(name)s %(reqid)s: %(message)s"

_ring: RingBufferHandler | None = None
_log_file_path: str | None = None
_configured_level: str = DEFAULT_LEVEL
_setup_lock = threading.Lock()


# --- request-id context ------------------------------------------------------


class _RequestContext(threading.local):
    request_id: int | None = None


_context = _RequestContext()


def set_request_id(value: int | None) -> None:
    """Tag subsequent log lines on THIS thread with an RPC request number."""
    _context.request_id = value


def request_id() -> int | None:
    return getattr(_context, "request_id", None)


def clear_request_id() -> None:
    _context.request_id = None


class _RequestIdFilter(logging.Filter):
    """Inject ``record.reqid`` so the format string can reference it."""

    def filter(self, record: logging.LogRecord) -> bool:
        value = getattr(_context, "request_id", None)
        record.reqid = f"req#{value}" if value is not None else "-"
        return True


# --- helpers -----------------------------------------------------------------


def _truncate(text: str, limit: int = MAX_FIELD_CHARS) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATE_SUFFIX)] + TRUNCATE_SUFFIX


def _iso(created: float) -> str:
    return datetime.fromtimestamp(created).isoformat(timespec="milliseconds")


def _level_value(name: Any) -> int:
    if isinstance(name, int):
        return name
    value = logging.getLevelName(str(name).upper())
    return value if isinstance(value, int) else logging.NOTSET


def _resolve_level(level: Any) -> str | None:
    """Explicit argument, else ``CADPILOT_LOG_LEVEL``; None when neither is usable."""
    candidate = level or os.environ.get("CADPILOT_LOG_LEVEL")
    if not candidate:
        return None
    name = str(candidate).upper()
    return name if name in LEVELS else None


def summarize_args(args: Any, max_chars: int = MAX_FIELD_CHARS) -> str:
    """One-line, size-capped rendering of an RPC call's arguments.

    Structured values collapse to their shape (dict keys, list length) because
    a full ``obj_properties`` or sketch spec would flood the log and put its
    serialization cost on whichever thread is logging.
    """
    parts = []
    for value in args or ():
        if isinstance(value, dict):
            keys = ",".join(list(value)[:8])
            parts.append("{" + keys + ("..." if len(value) > 8 else "") + "}")
        elif isinstance(value, (list, tuple)):
            parts.append(f"[{len(value)} items]")
        elif isinstance(value, str):
            parts.append(value if len(value) <= 60 else value[:57] + TRUNCATE_SUFFIX)
        else:
            parts.append(repr(value))
    return _truncate(", ".join(parts), max_chars)


def _split_detail(record: logging.LogRecord) -> tuple[str, str]:
    """(message, detail) where detail is a traceback and/or an ``extra`` dump."""
    detail = ""
    if record.exc_info:
        detail = logging.Formatter().formatException(record.exc_info)
    extra = getattr(record, "detail", None)
    if extra is not None:
        try:
            rendered = json.dumps(extra, ensure_ascii=False, default=str)
        except Exception:
            rendered = str(extra)
        detail = f"{detail}\n{rendered}" if detail else rendered
    return record.getMessage(), detail


def default_log_dir() -> str:
    override = os.environ.get("CADPILOT_LOG_DIR")
    if override:
        return override
    base = None
    if FreeCAD is not None:
        try:
            base = FreeCAD.getUserAppDataDir()
        except Exception:
            base = None
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".cadpilot")
    return os.path.join(base, "CADPilot", "logs")


# --- handlers ----------------------------------------------------------------


class RingBufferHandler(logging.Handler):
    """Bounded in-memory record store; the data source for ``get_addon_log``."""

    def __init__(self, capacity: int = DEFAULT_RING_SIZE):
        super().__init__(logging.DEBUG)
        self._records: deque[dict[str, Any]] = deque(maxlen=max(1, int(capacity)))
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message, detail = _split_detail(record)
            with self._lock:
                self._seq += 1
                self._records.append(
                    {
                        "seq": self._seq,
                        "time": _iso(record.created),
                        "level": record.levelname,
                        "name": record.name,
                        "thread": record.threadName,
                        "request": getattr(record, "reqid", "-"),
                        "message": _truncate(message),
                        "detail": _truncate(detail, MAX_DETAIL_CHARS),
                    }
                )
        except Exception:
            self.handleError(record)

    def snapshot(
        self,
        level: Any = None,
        grep: str | None = None,
        since_seq: int = 0,
        limit: int | None = 100,
    ) -> list[dict[str, Any]]:
        threshold = _level_value(level) if level else None
        needle = (grep or "").lower()
        with self._lock:
            records = list(self._records)
        out = []
        for rec in records:
            if rec["seq"] <= since_seq:
                continue
            if threshold is not None and _level_value(rec["level"]) < threshold:
                continue
            if (
                needle
                and needle not in rec["message"].lower()
                and needle not in rec["detail"].lower()
            ):
                continue
            out.append(rec)
        if limit is not None:
            out = out[-limit:] if limit > 0 else []
        return out

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


class ReportViewHandler(logging.Handler):
    """Mirror WARNING+ to FreeCAD's Report View; a no-op without FreeCAD."""

    def __init__(self):
        super().__init__(logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        if FreeCAD is None:
            return
        try:
            if record.levelno >= logging.ERROR:
                sink = FreeCAD.Console.PrintError
            else:
                sink = FreeCAD.Console.PrintWarning
            sink(f"[CADPilot] {record.getMessage()}\n")
            if record.exc_info:
                FreeCAD.Console.PrintError(logging.Formatter().formatException(record.exc_info))
        except Exception:
            self.handleError(record)


# --- configuration -----------------------------------------------------------


def _settings_overrides() -> tuple[Any, Any, Any]:
    """Best-effort read of the persisted settings; never raises."""
    try:
        from rpc_server.settings import load_settings

        settings = load_settings()
        return settings.get("log_level"), settings.get("log_dir"), settings.get("log_ring_size")
    except Exception:
        return None, None, None


def setup_logging(
    level: Any = None,
    log_dir: str | None = None,
    ring_size: int | None = None,
    force: bool = False,
) -> str:
    """Build the CADPilot logger pipeline once. Returns the effective level.

    Precedence for the level: explicit argument > ``CADPILOT_LOG_LEVEL`` >
    saved setting > the level already in force > ``DEFAULT_LEVEL``. Re-running
    setup therefore never silently downgrades a level someone raised.

    Idempotent (callers are module imports, toolbar handlers and the RPC thread
    alike) and it never raises — a logging failure must not take the addon
    down, so an unwritable directory just drops the file handler and keeps the
    ring buffer.
    """
    global _ring, _log_file_path, _configured_level
    with _setup_lock:
        settings_level, settings_dir, settings_ring = _settings_overrides()
        wanted = _resolve_level(level) or _resolve_level(settings_level)
        if wanted is None and _ring is not None:
            wanted = _configured_level
        resolved = wanted or DEFAULT_LEVEL

        logger = logging.getLogger(ROOT_LOGGER)
        # The logger always passes DEBUG and the HANDLERS decide what is worth
        # keeping. Gating at the logger instead would mean a DEBUG record is
        # never created, so get_addon_log(level="DEBUG") could never return
        # anything — the one thing the tool exists for. `log_level` gates the
        # file and the Report View; the ring buffer is the always-on firehose.
        logger.setLevel(logging.DEBUG)
        # Our handlers own the output; letting records propagate would also
        # hand them to FreeCAD's root logger and duplicate every line.
        logger.propagate = False
        _configured_level = resolved

        if _ring is not None and not force:
            _apply_handler_levels()
            return _configured_level

        for handler in list(logger.handlers):
            logger.removeHandler(handler)

        req_filter = _RequestIdFilter()

        try:
            effective_ring = int(ring_size or settings_ring or DEFAULT_RING_SIZE)
        except (TypeError, ValueError):
            # setup_logging must never raise: a corrupt (hand-edited) settings
            # value used to kill setup_logging, and get_logger runs at request_log
            # IMPORT time, so the whole addon never came up.
            effective_ring = DEFAULT_RING_SIZE
        _ring = RingBufferHandler(effective_ring)
        _ring.addFilter(req_filter)
        logger.addHandler(_ring)

        _log_file_path = None
        try:
            directory = log_dir or os.environ.get("CADPILOT_LOG_DIR") or settings_dir
            directory = directory or default_log_dir()
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, LOG_FILENAME)
            file_handler = logging.handlers.RotatingFileHandler(
                path,
                maxBytes=DEFAULT_MAX_BYTES,
                backupCount=DEFAULT_BACKUPS,
                encoding="utf-8",
                delay=True,
            )
            file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
            file_handler.addFilter(req_filter)
            logger.addHandler(file_handler)
            _log_file_path = path
        except Exception:
            _log_file_path = None

        report = ReportViewHandler()
        report.addFilter(req_filter)
        logger.addHandler(report)

        _apply_handler_levels()
        return _configured_level


def _apply_handler_levels() -> None:
    """Route the configured threshold to the durable handlers only.

    The ring buffer stays at DEBUG: it is bounded, so capturing everything is
    cheap and it is what makes an on-demand ``get_addon_log(level="DEBUG")``
    possible without reconfiguring anything. The Report View never goes below
    WARNING — it is meant for things a human should notice, not a firehose.
    """
    threshold = _level_value(_configured_level)
    for handler in logging.getLogger(ROOT_LOGGER).handlers:
        if isinstance(handler, RingBufferHandler):
            handler.setLevel(logging.DEBUG)
        elif isinstance(handler, ReportViewHandler):
            handler.setLevel(max(logging.WARNING, threshold))
        else:
            handler.setLevel(threshold)


def set_level(level: Any) -> str:
    """Change what reaches the file and Report View; returns the level in force."""
    global _configured_level
    resolved = _resolve_level(level)
    if resolved is None:
        return _configured_level
    _configured_level = resolved
    _apply_handler_levels()
    return _configured_level


def get_logger(suffix: str = "") -> logging.Logger:
    """The addon's logger; sets the pipeline up on first use."""
    if _ring is None:
        setup_logging()
    return logging.getLogger(f"{ROOT_LOGGER}.{suffix}" if suffix else ROOT_LOGGER)


# --- reading -----------------------------------------------------------------


def query(
    level: Any = None,
    grep: str | None = None,
    since_seq: int = 0,
    limit: int | None = 100,
) -> list[dict[str, Any]]:
    """Read buffered records, newest last. Empty before setup has run."""
    if _ring is None:
        return []
    try:
        return _ring.snapshot(level=level, grep=grep, since_seq=since_seq, limit=limit)
    except Exception:
        return []


def clear() -> None:
    if _ring is not None:
        _ring.clear()


def status() -> dict[str, Any]:
    """Where the log goes and how much is buffered — for diagnostics itself."""
    return {
        "level": _configured_level,
        "capture": "DEBUG",  # the ring buffer always records at this level
        "setup_done": _ring is not None,
        "buffered": len(_ring._records) if _ring is not None else 0,
        "capacity": _ring._records.maxlen if _ring is not None else 0,
        "log_file": _log_file_path,
        "request_id": request_id(),
    }
