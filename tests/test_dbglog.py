"""Debug log core — pure logic, no FreeCAD needed."""

import logging
import sys
from pathlib import Path

_ADDON = Path(__file__).resolve().parents[1] / "addon" / "CADPilot"
if str(_ADDON) not in sys.path:
    sys.path.insert(0, str(_ADDON))

import pytest  # noqa: E402
from rpc_server import dbglog  # noqa: E402


@pytest.fixture
def fresh_log(tmp_path, monkeypatch):
    """A configured pipeline writing to a temp dir, reset afterwards."""
    monkeypatch.delenv("CADPILOT_LOG_LEVEL", raising=False)
    monkeypatch.delenv("CADPILOT_LOG_DIR", raising=False)
    level = dbglog.setup_logging(
        level="DEBUG", log_dir=str(tmp_path / "logs"), ring_size=1000, force=True
    )
    assert level == "DEBUG"
    yield dbglog.get_logger("test"), tmp_path / "logs"
    dbglog.clear()


def test_records_carry_the_expected_shape(fresh_log):
    logger, _ = fresh_log
    logger.info("hello")
    rec = dbglog.query(limit=1)[0]
    assert set(rec) == {"seq", "time", "level", "name", "thread", "request", "message", "detail"}
    assert rec["message"] == "hello"
    assert rec["level"] == "INFO"
    assert rec["name"] == "CADPilot.test"
    assert rec["request"] == "-"


def test_ring_buffer_is_bounded_and_drops_the_oldest(tmp_path, monkeypatch):
    monkeypatch.delenv("CADPILOT_LOG_LEVEL", raising=False)
    dbglog.setup_logging(level="DEBUG", log_dir=str(tmp_path), ring_size=5, force=True)
    logger = dbglog.get_logger("capped")
    for i in range(20):
        logger.info("capped %d", i)
    recs = dbglog.query(limit=100)
    assert len(recs) == 5
    assert [r["message"] for r in recs] == [f"capped {i}" for i in range(15, 20)]
    dbglog.clear()


def test_level_filtering(fresh_log):
    logger, _ = fresh_log
    logger.debug("d")
    logger.warning("w")
    assert [r["message"] for r in dbglog.query(level="WARNING")] == ["w"]
    assert [r["message"] for r in dbglog.query(level="DEBUG")] == ["d", "w"]
    # An unknown level must not silently filter everything out.
    assert [r["message"] for r in dbglog.query(level="NONSENSE")] == ["d", "w"]


def test_grep_matches_message_and_detail(fresh_log):
    logger, _ = fresh_log
    logger.info("object created")
    logger.info("plain", extra={"detail": {"secret": "needle"}})
    assert [r["message"] for r in dbglog.query(grep="NEEDLE")] == ["plain"]
    assert [r["message"] for r in dbglog.query(grep="created")] == ["object created"]


def test_since_seq_and_limit_take_the_newest(fresh_log):
    logger, _ = fresh_log
    for i in range(5):
        logger.info("n%d", i)
    recs = dbglog.query(limit=100)
    assert [r["message"] for r in dbglog.query(since_seq=recs[2]["seq"])] == ["n3", "n4"]
    assert [r["message"] for r in dbglog.query(limit=2)] == ["n3", "n4"]
    assert dbglog.query(limit=0) == []


def test_long_fields_are_truncated(fresh_log):
    logger, _ = fresh_log
    logger.info("x" * 5000)
    rec = dbglog.query(limit=1)[0]
    assert len(rec["message"]) <= dbglog.MAX_FIELD_CHARS
    assert rec["message"].endswith(dbglog.TRUNCATE_SUFFIX)


def test_exception_records_capture_a_traceback(fresh_log):
    logger, _ = fresh_log
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("failed")
    rec = dbglog.query(limit=1)[0]
    assert rec["level"] == "ERROR"
    assert "ValueError: boom" in rec["detail"]
    assert "Traceback" in rec["detail"]


def test_request_id_is_injected_and_cleared(fresh_log):
    logger, _ = fresh_log
    dbglog.set_request_id(7)
    logger.info("with id")
    dbglog.clear_request_id()
    logger.info("without id")
    recs = dbglog.query(limit=2)
    assert recs[0]["request"] == "req#7"
    assert recs[1]["request"] == "-"


def test_setup_is_idempotent_and_does_not_stack_handlers(fresh_log):
    _logger, log_dir = fresh_log
    root = logging.getLogger(dbglog.ROOT_LOGGER)
    before = len(root.handlers)
    dbglog.setup_logging(log_dir=str(log_dir))
    dbglog.setup_logging(log_dir=str(log_dir))
    assert len(root.handlers) == before


def test_repeated_setup_does_not_reset_an_explicitly_set_level(fresh_log):
    """get_logger() and any later setup call must not clobber set_level()."""
    _logger, log_dir = fresh_log
    dbglog.set_level("WARNING")
    dbglog.setup_logging(log_dir=str(log_dir))
    assert dbglog.status()["level"] == "WARNING"
    assert _file_handler_level() == logging.WARNING


def _file_handler_level():
    for handler in logging.getLogger(dbglog.ROOT_LOGGER).handlers:
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            return handler.level
    raise AssertionError("no file handler installed")


def test_debug_records_are_captured_even_at_an_info_configuration(tmp_path, monkeypatch):
    """Otherwise get_addon_log(level='DEBUG') could never return anything.

    The configured level gates the file and the Report View; the ring buffer is
    the always-on capture that makes on-demand DEBUG reads possible.
    """
    monkeypatch.delenv("CADPILOT_LOG_LEVEL", raising=False)
    dbglog.setup_logging(level="INFO", log_dir=str(tmp_path), ring_size=100, force=True)
    dbglog.clear()
    logger = dbglog.get_logger("capture")
    logger.debug("noisy detail")
    logger.info("visible line")

    assert dbglog.status()["level"] == "INFO"
    assert [r["message"] for r in dbglog.query(level="DEBUG")] == ["noisy detail", "visible line"]
    # ...but at INFO the debug record must not reach the durable file.
    for handler in logging.getLogger(dbglog.ROOT_LOGGER).handlers:
        handler.flush()
    text = (tmp_path / dbglog.LOG_FILENAME).read_text(encoding="utf-8")
    assert "visible line" in text
    assert "noisy detail" not in text
    dbglog.clear()


def test_report_view_never_goes_below_warning(tmp_path, monkeypatch):
    monkeypatch.delenv("CADPILOT_LOG_LEVEL", raising=False)
    dbglog.setup_logging(level="DEBUG", log_dir=str(tmp_path), force=True)
    for handler in logging.getLogger(dbglog.ROOT_LOGGER).handlers:
        if isinstance(handler, dbglog.ReportViewHandler):
            assert handler.level == logging.WARNING
            break
    else:
        raise AssertionError("no Report View handler installed")
    dbglog.clear()


def test_setup_writes_a_rotating_file(fresh_log):
    logger, log_dir = fresh_log
    logger.warning("to disk")
    for handler in logging.getLogger(dbglog.ROOT_LOGGER).handlers:
        handler.flush()
    text = (log_dir / dbglog.LOG_FILENAME).read_text(encoding="utf-8")
    assert "to disk" in text
    assert "WARNING" in text
    assert "CADPilot.test" in text  # the logger name, so lines are attributable


def test_file_lines_carry_the_request_id(fresh_log):
    logger, log_dir = fresh_log
    dbglog.set_request_id(3)
    logger.warning("tagged")
    dbglog.clear_request_id()
    for handler in logging.getLogger(dbglog.ROOT_LOGGER).handlers:
        handler.flush()
    text = (log_dir / dbglog.LOG_FILENAME).read_text(encoding="utf-8")
    assert "req#3" in text


def test_child_loggers_do_not_reach_the_root_logger(fresh_log):
    """propagate=False: FreeCAD's own root logging must not duplicate our lines."""
    assert logging.getLogger(dbglog.ROOT_LOGGER).propagate is False


def test_query_before_setup_returns_empty(monkeypatch):
    monkeypatch.setattr(dbglog, "_ring", None)
    assert dbglog.query() == []
    assert dbglog.status()["setup_done"] is False


def test_level_resolution(monkeypatch):
    monkeypatch.setenv("CADPILOT_LOG_LEVEL", "warning")
    assert dbglog._resolve_level(None) == "WARNING"
    assert dbglog._resolve_level("debug") == "DEBUG"  # explicit arg wins
    monkeypatch.setenv("CADPILOT_LOG_LEVEL", "nope")
    assert dbglog._resolve_level(None) is None
    monkeypatch.delenv("CADPILOT_LOG_LEVEL")
    assert dbglog._resolve_level(None) is None


def test_default_log_dir_honours_the_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CADPILOT_LOG_DIR", str(tmp_path / "custom"))
    assert dbglog.default_log_dir() == str(tmp_path / "custom")


def test_summarize_args_is_one_capped_line():
    out = dbglog.summarize_args(["Doc", {"a": 1, "b": 2}, [1, 2, 3], "x" * 200])
    assert "\n" not in out
    assert "Doc" in out and "{a,b}" in out and "[3 items]" in out
    assert len(out) <= dbglog.MAX_FIELD_CHARS


def test_setup_never_raises_on_an_unwritable_dir(tmp_path):
    """A logging failure must not take the addon down."""
    blocked = tmp_path / "file"
    blocked.write_text("not a directory", encoding="utf-8")
    level = dbglog.setup_logging(level="INFO", log_dir=str(blocked), force=True)
    assert level == "INFO"
    assert dbglog.status()["setup_done"] is True
    assert dbglog.status()["log_file"] is None  # file handler dropped, ring kept
    dbglog.clear()


def test_traceback_detail_gets_more_room_than_the_message(fresh_log):
    """A clipped traceback is barely worth reading; `detail` has its own cap."""
    logger, _ = fresh_log
    try:
        raise ValueError("x" * 800)
    except ValueError:
        logger.exception("failed")
    rec = dbglog.query(limit=1)[0]
    assert len(rec["detail"]) > dbglog.MAX_FIELD_CHARS
    assert len(rec["detail"]) <= dbglog.MAX_DETAIL_CHARS


def test_long_extra_payload_is_capped_at_the_detail_limit(fresh_log):
    logger, _ = fresh_log
    logger.info("with payload", extra={"detail": {"blob": "y" * 10000}})
    rec = dbglog.query(limit=1)[0]
    assert len(rec["detail"]) <= dbglog.MAX_DETAIL_CHARS
    assert rec["detail"].endswith(dbglog.TRUNCATE_SUFFIX)
