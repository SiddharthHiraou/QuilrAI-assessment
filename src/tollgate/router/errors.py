"""The only error shapes the LLM gateway ever sends.

A closed vocabulary with fixed messages. Upstream error bodies routinely carry
internal request ids, org ids, shard names and sometimes a slice of the prompt,
so nothing from them is copied into a client response. The detail goes to the
log under the same request_id the client gets.
"""

import json

from starlette.responses import JSONResponse

ERRORS = {
    "invalid_request": (400, "The request body is not a valid chat completion request."),
    "request_too_large": (400, "This request needs more tokens than the per-minute budget allows."),
    "authentication_error": (401, "Missing or invalid API key."),
    "not_found": (404, "No such endpoint."),
    "method_not_allowed": (405, "Method not allowed on this endpoint."),
    "rate_limited": (429, "Token rate limit reached for this API key."),
    "upstream_rejected": (400, "The model provider rejected the request as invalid."),
    "providers_unavailable": (503, "No model provider could serve this request."),
    "stream_interrupted": (502, "The model provider stopped responding mid-stream."),
    "internal_error": (500, "Internal gateway error."),
}


def error_body(kind, request_id, **extra):
    status, message = ERRORS[kind]
    return status, {"error": {"type": kind, "message": message, "request_id": request_id, **extra}}


def error_response(kind, request_id, headers=None, **extra):
    status, body = error_body(kind, request_id, **extra)
    return JSONResponse(body, status_code=status,
                        headers={"x-request-id": request_id, **(headers or {})})


def error_event(kind, request_id):
    """Same payload as an SSE frame, for failures after the stream started."""
    _, body = error_body(kind, request_id)
    return ("data: " + json.dumps(body) + "\n\n").encode()
