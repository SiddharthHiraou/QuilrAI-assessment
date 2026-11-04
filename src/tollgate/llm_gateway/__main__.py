"""python -m tollgate.llm_gateway [--port 8830]"""

import argparse
import os

import uvicorn

from tollgate.llm_gateway.app import create_app
from tollgate.llm_gateway.config import Config
from tollgate.log import Log

log = Log("llm_gateway")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m tollgate.llm_gateway")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8830)
    args = p.parse_args(argv)

    config = Config()
    os.makedirs(os.path.dirname(config.db_path) or ".", exist_ok=True)
    log.info("ready", port=args.port, providers=[p.name for p in config.providers],
             tpm_limit=config.tpm_limit, db=config.db_path, redact=config.redact)
    uvicorn.run(create_app(config), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
