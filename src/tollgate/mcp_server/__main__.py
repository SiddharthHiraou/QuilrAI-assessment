"""python -m tollgate.mcp_server [--http PORT] [--admin-tools]

Default is stdio, which is what task 1 asks for. --http serves the same tools
over Streamable HTTP so the gateway in task 2 has a real MCP server to sit in
front of instead of a hand-written mock.
"""

import argparse

import anyio
from mcp.server.stdio import stdio_server

from tollgate.log import Log
from tollgate.mcp_server.server import build_server
from tollgate.mcp_server.stdio import AnswerUnparseable, stdout_is_stderr

log = Log("mcp_server")


async def serve_stdio(admin_tools):
    server = build_server(admin_tools=admin_tools)
    async with stdio_server() as (read, write):
        with stdout_is_stderr():
            log.info("ready", transport="stdio")
            await server.run(AnswerUnparseable(read, write), write,
                             server.create_initialization_options())


def serve_http(host, port, admin_tools):
    import uvicorn

    server = build_server(admin_tools=admin_tools)
    # On a loopback host the SDK turns on DNS rebinding protection by itself.
    # Bound wider (inside docker compose) it's reachable only from the private
    # network behind the gateway, which is where the Origin check lives.
    app = server.streamable_http_app(host=host)
    log.info("ready", transport="streamable-http", host=host, port=port)
    uvicorn.run(app, host=host, port=port, log_level="warning")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m tollgate.mcp_server")
    p.add_argument("--http", type=int, metavar="PORT", help="serve Streamable HTTP on this port")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--admin-tools", action="store_true", help="also expose admin_* tools")
    args = p.parse_args(argv)

    if args.http:
        serve_http(args.host, args.http, args.admin_tools)
        return
    try:
        anyio.run(serve_stdio, args.admin_tools)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
