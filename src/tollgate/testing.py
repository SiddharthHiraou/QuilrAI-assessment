"""Helpers for the tests and the demo: real sockets, and a downstream recorder.

serving() runs an ASGI app on a real port.

httpx's ASGITransport buffers a response to completion before handing it over,
so it would happily pass a gateway that accumulates the whole stream. Anything
asserting on streaming behaviour or timing goes through a real port instead.
"""

import contextlib
import json
import socket
import threading
import time

import uvicorn


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def serving(app, port=None):
    port = port or free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("test server didn't start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


class Recorder:
    """ASGI wrapper that remembers what reached the downstream."""

    def __init__(self, app):
        self.app = app
        self.messages = []
        self.headers = []

    def tools_called(self):
        return [m["params"]["name"] for m in self.messages
                if isinstance(m, dict) and m.get("method") == "tools/call"]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        body, more = b"", True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        self.headers.append({k.decode(): v.decode() for k, v in scope["headers"]})
        if body:
            payload = json.loads(body)
            self.messages.extend(payload if isinstance(payload, list) else [payload])
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()
        await self.app(scope, replay, send)
