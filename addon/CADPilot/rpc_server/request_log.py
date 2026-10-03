"""XML-RPC server with per-request logging.

``SimpleXMLRPCDispatcher._dispatch`` is the single choke point every RPC call
passes through, so instrumenting it here covers all handlers at once — and it
is also where the request id is minted, which ``gui_dispatch`` then carries
onto the GUI thread so a call's deferred work stays correlatable.
"""

from __future__ import annotations

import itertools
import time

from rpc_server import dbglog
from rpc_server.ip_filter import FilteredXMLRPCServer

logger = dbglog.get_logger("rpc")

_counter = itertools.count(1)

# A call slower than this is a queueing symptom, not a slow operation: the
# addon runs every FreeCAD touch on one GUI thread, so latency here is usually
# time spent waiting behind someone else.
SLOW_CALL_MS = 1000.0


class LoggedXMLRPCServer(FilteredXMLRPCServer):
    """Filtered server that logs entry, duration and failure of every request."""

    def _dispatch(self, method, params):
        request = next(_counter)
        dbglog.set_request_id(request)
        started = time.monotonic()
        # Entry/exit stays at INFO: one line per request is the backbone of
        # "what did the addon actually do", and a log that is empty at its
        # default level cannot answer that. The argument summary is the noisy
        # part, so it is DEBUG-only.
        logger.info("-> %s", method)
        logger.debug("   args: %s", dbglog.summarize_args(params))
        try:
            result = super()._dispatch(method, params)
        except Exception:
            # exc_info=True, not a formatted traceback in the message: the
            # message field is size-capped for flooding, and a truncated
            # traceback is close to useless. `detail` has room for it.
            logger.error(
                "!! %s failed after %.1fms",
                method,
                (time.monotonic() - started) * 1000,
                exc_info=True,
            )
            raise
        elapsed = (time.monotonic() - started) * 1000
        # A slow call is a queueing symptom, not a slow operation: the addon
        # runs every FreeCAD touch on one GUI thread, so latency here is
        # usually time spent waiting behind someone else.
        if elapsed > SLOW_CALL_MS:
            logger.warning("<- %s took %.1fms", method, elapsed)
        else:
            logger.info("<- %s ok in %.1fms", method, elapsed)
        return result

    def _marshaled_dispatch(self, data, dispatch_method=None, path=None):
        try:
            return super()._marshaled_dispatch(data, dispatch_method, path)
        finally:
            # The handler thread is reused across requests; a stale id would
            # mislabel every later line on it.
            dbglog.clear_request_id()
