"""JSON-RPC 2.0 envelope handling for the gateway."""

import re

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# Implementation-defined band is -32000..-32019. -32001 is the brief's.
# -32002 is skipped: the 2026-07-28 allocation policy lists it as retired and
# never to be reused.
UNAUTHORIZED_TOOL_CALL = -32001
DOWNSTREAM_UNAVAILABLE = -32003
# Spec-defined (2026-07-28): routing headers disagree with the body.
HEADER_MISMATCH = -32020

# Tool names outside this set are refused rather than matched against policy.
# Anything that needs Unicode normalisation or case games to look like an
# admin tool never gets the chance.
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.\-/]{1,128}$")


def error(msg_id, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def message_id(msg):
    """The id to answer with. Only str/int ids are real ones; anything else is null."""
    if isinstance(msg, dict):
        value = msg.get("id")
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            return value
    return None


def kind(msg):
    """request, notification, response, or None if it isn't a JSON-RPC message."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return None
    if "method" in msg:
        if not isinstance(msg["method"], str):
            return None
        if "id" not in msg:
            return "notification"
        # id: 0 is a perfectly good id. A truthiness check would treat it as a
        # notification and swallow the reply.
        return "request" if message_id(msg) is not None else None
    if ("result" in msg) != ("error" in msg) and "id" in msg:
        return "response"
    return None
