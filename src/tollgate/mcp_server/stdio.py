"""stdio plumbing the SDK doesn't do for us."""

import contextlib
import sys

import anyio
from mcp.shared.message import SessionMessage
from mcp_types import INVALID_REQUEST, PARSE_ERROR, ErrorData, JSONRPCError
from pydantic import ValidationError

from tollgate.log import Log

log = Log("mcp_server.stdio")


@contextlib.contextmanager
def stdout_is_stderr():
    """Point the sys.stdout object at stderr while the transport owns fd 1.

    stdio_server() already handles the descriptor level: it dups fd 1 to a
    private fd for the wire and points fd 1 at stderr, so os.write(1) and child
    processes are covered. This covers code holding the sys.stdout object, and
    the SDK's fallback path where the fd dance fails and it serves the wire
    from sys.stdout's own buffer.

    Has to be entered INSIDE stdio_server(), never around it. stdio_server()
    checks sys.stdout is backed by fd 1 before claiming the fd. Swap it first
    and that check fails, the claim is skipped, and the server writes the wire
    into stderr. It logs "ready" and every client then waits forever.
    """
    saved = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = saved


def error_for_unparseable(exc):
    errors = exc.errors() if isinstance(exc, ValidationError) else []
    if any(e["type"] == "json_invalid" for e in errors):
        return JSONRPCError(jsonrpc="2.0", id=None, error=ErrorData(code=PARSE_ERROR, message="Parse error"))

    # Valid JSON, not a valid message. Hand back the id when there's an
    # unambiguous one so the client can fail that call instead of waiting on
    # it; JSON-RPC says null when it can't be determined.
    msg_id = None
    for e in errors:
        top = e.get("input")
        if isinstance(top, dict) and ("method" in top or "jsonrpc" in top):
            candidate = top.get("id")
            if isinstance(candidate, (str, int)) and not isinstance(candidate, bool):
                msg_id = candidate
            break
    return JSONRPCError(jsonrpc="2.0", id=msg_id, error=ErrorData(code=INVALID_REQUEST, message="Invalid Request"))


class AnswerUnparseable:
    """Wraps the transport's read stream and answers lines that aren't JSON-RPC.

    The SDK's stdin reader turns a bad line into an Exception on the read
    stream, and the serve loop drops it on the floor. JSON-RPC 2.0 wants a
    -32700 (bad JSON) or -32600 (bad shape) response, and a client that sent a
    truncated frame otherwise just waits for a reply that never comes.
    """

    def __init__(self, inner, write_stream):
        self._inner = inner
        self._write = write_stream

    @property
    def last_context(self):
        # The serve loop restores the sender's contextvars from this.
        return getattr(self._inner, "last_context", None)

    async def receive(self):
        while True:
            item = await self._inner.receive()
            if not isinstance(item, Exception):
                return item
            reply = error_for_unparseable(item)
            log.warning("unparseable message", code=reply.error.code)
            await self._write.send(SessionMessage(reply))

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.receive()
        except anyio.EndOfStream:
            raise StopAsyncIteration from None

    def close(self):
        self._inner.close()

    async def aclose(self):
        await self._inner.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()
