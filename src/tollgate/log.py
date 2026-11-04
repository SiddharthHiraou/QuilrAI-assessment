"""JSON-lines logging to stderr.

stdout is the JSON-RPC wire for the stdio MCP server, so nothing in this repo
logs there, even the HTTP services. One rule everywhere is easier than
remembering which process is allowed to print.
"""

import json
import logging
import os
import sys

_configured = False


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        out = {
            "ts": round(record.created, 3),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Fields travel under one key so they can never collide with LogRecord
        # attributes. extra={"name": ...} raises KeyError inside logging, which
        # took down an entry point once before I started doing it this way.
        out.update(getattr(record, "fields", None) or {})
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def _configure():
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger("tollgate")
    root.addHandler(handler)
    root.setLevel(os.environ.get("TOLLGATE_LOG_LEVEL", "INFO").upper())
    root.propagate = False
    _configured = True


class Log:
    def __init__(self, name):
        _configure()
        self._log = logging.getLogger("tollgate." + name)

    def _emit(self, level, msg, fields, exc_info=False):
        self._log.log(level, msg, extra={"fields": fields}, exc_info=exc_info)

    def debug(self, msg, **fields):
        self._emit(logging.DEBUG, msg, fields)

    def info(self, msg, **fields):
        self._emit(logging.INFO, msg, fields)

    def warning(self, msg, **fields):
        self._emit(logging.WARNING, msg, fields)

    def error(self, msg, **fields):
        self._emit(logging.ERROR, msg, fields)

    def exception(self, msg, **fields):
        self._emit(logging.ERROR, msg, fields, exc_info=True)
