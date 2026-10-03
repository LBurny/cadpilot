"""RPC request logging: the log must not be empty at its default level.

The addon's log is the only record of what it did while the caller only saw a
tool response, so the level chosen at each call site is part of the contract:
entry/exit/transactions at INFO, argument summaries and the 500ms mouse-guard
deferrals at DEBUG. These tests pin that split.
"""

import sys
import xmlrpc.client
from pathlib import Path

_ADDON = Path(__file__).resolve().parents[1] / "addon" / "CADPilot"
if str(_ADDON) not in sys.path:
    sys.path.insert(0, str(_ADDON))

import pytest  # noqa: E402
from rpc_server import dbglog, request_log  # noqa: E402


class _StubServer(request_log.LoggedXMLRPCServer):
    """Skips socket setup: SimpleXMLRPCDispatcher._dispatch needs only funcs."""

    def __init__(self):
        self.funcs = {"ping": lambda *args: "pong"}
        self.instance = None
        # Needed by _marshaled_dispatch to build its response.
        self.allow_none = True
        self.encoding = "utf-8"
        self.use_builtin_types = False


@pytest.fixture
def rpc_log(tmp_path, monkeypatch):
    monkeypatch.delenv("CADPILOT_LOG_LEVEL", raising=False)
    dbglog.setup_logging(level="DEBUG", log_dir=str(tmp_path), ring_size=500, force=True)
    dbglog.clear()
    yield _StubServer()
    # _dispatch sets the request id; only _marshaled_dispatch clears it (which
    # the stub does not exercise), so the test has to clean up after itself.
    dbglog.clear_request_id()
    dbglog.clear()


def _messages(level=None):
    return [r["message"] for r in dbglog.query(level=level)]


def test_entry_and_exit_are_visible_at_the_default_info_level(rpc_log):
    assert rpc_log._dispatch("ping", ()) == "pong"

    info = _messages(level="INFO")
    assert "-> ping" in info
    assert any(m.startswith("<- ping ok in") for m in info)


def test_argument_summary_is_debug_only(rpc_log):
    rpc_log._dispatch("ping", ("Doc", {"a": 1}))

    assert not any("args:" in m for m in _messages(level="INFO"))
    assert any("args: Doc, {a}" in m for m in _messages(level="DEBUG"))


def test_a_failed_call_logs_the_traceback(rpc_log):
    with pytest.raises(Exception, match="no_such_method"):
        rpc_log._dispatch("no_such_method", ())

    rec = dbglog.query(level="ERROR")[0]
    assert rec["message"].startswith("!! no_such_method failed after")
    assert "Traceback" in rec["detail"]
    assert "is not supported" in rec["detail"]


def test_records_carry_the_request_id_of_the_call(rpc_log):
    rpc_log._dispatch("ping", ())
    rec = dbglog.query(level="INFO")[0]
    assert rec["request"].startswith("req#")
    assert rec["request"] != "-"


def test_each_call_gets_its_own_request_id(rpc_log):
    rpc_log._dispatch("ping", ())
    rpc_log._dispatch("ping", ())
    ids = {r["request"] for r in dbglog.query(level="INFO")}
    assert len(ids) == 2


def test_marshaled_dispatch_clears_the_request_id(rpc_log):
    """The handler thread is reused; a stale id would mislabel later requests."""
    body = xmlrpc.client.dumps(("ping",), methodname="ping").encode()
    rpc_log._marshaled_dispatch(body)

    assert dbglog.request_id() is None


def test_marshaled_dispatch_clears_the_request_id_even_on_failure(rpc_log):
    body = xmlrpc.client.dumps((), methodname="no_such_method").encode()
    rpc_log._marshaled_dispatch(body)  # returns a Fault response, does not raise

    assert dbglog.request_id() is None
