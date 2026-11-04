"""python -m tollgate.mcp_gateway [--port 8800]"""

import argparse

import uvicorn

from tollgate.mcp_gateway.app import create_app


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m tollgate.mcp_gateway")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8800)
    args = p.parse_args(argv)
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
