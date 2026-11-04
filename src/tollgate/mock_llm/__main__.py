"""python -m tollgate.mock_llm --name primary --port 8831 [--mode ok]"""

import argparse

import uvicorn

from tollgate.mock_llm.app import create_app


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m tollgate.mock_llm")
    p.add_argument("--name", default="mock")
    p.add_argument("--port", type=int, default=8831)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--mode", default="ok", choices=["ok", "rate_limit", "error", "slow", "stall", "reject"])
    p.add_argument("--delay-ms", type=int, default=15)
    args = p.parse_args(argv)
    uvicorn.run(create_app(args.name, args.mode, args.delay_ms), host=args.host, port=args.port,
                log_level="warning")


if __name__ == "__main__":
    main()
