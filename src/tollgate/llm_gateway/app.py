"""OpenAI-compatible LLM gateway: auth, token budget, failover, PII redaction.

    client -> authenticate -> reserve tokens -> route (primary, then backup)
           <- redact deltas as they arrive <- provider stream
                                  settle the reservation with real usage

Tasks 3 and 4 meet here. The redactor and the router are separate modules and
test separately; this file is only the wiring and the HTTP edges.
"""

import asyncio
import contextlib
import hmac
import json
import math
import time
import uuid

import anyio
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from tollgate.guardrail.stream import OpenAIChat, redact_openai_completion
from tollgate.log import Log
from tollgate.router.errors import error_event, error_response
from tollgate.router.limiter import RateLimited, TokenLimiter, TooLargeForBudget, tenant_id
from tollgate.router.router import AllProvidersFailed, Router, UpstreamRejected
from tollgate.router.tokens import DEFAULT_MAX_TOKENS, estimate_prompt_tokens, tokens_for_chars

log = Log("llm_gateway")

MAX_COMPLETION_TOKENS = 16_384


def _bearer(request):
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else None


def _new_id():
    return "req_" + uuid.uuid4().hex[:16]


def _validate(body):
    """Enough shape checking to price the request. Everything else is the provider's call."""
    if not isinstance(body, dict):
        return "body must be a JSON object"
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return "messages must be a non-empty array"
    if not all(isinstance(m, dict) and isinstance(m.get("role"), str) for m in messages):
        return "every message needs a role"
    if "stream" in body and not isinstance(body["stream"], bool):
        return "stream must be a boolean"
    for key in ("max_tokens", "max_completion_tokens"):
        value = body.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                  or not 0 < value <= MAX_COMPLETION_TOKENS):
            return f"{key} must be an integer between 1 and {MAX_COMPLETION_TOKENS}"
    return None


def create_app(config, router=None, limiter=None):
    router = router or Router(config.providers)
    limiter = limiter or TokenLimiter(config.db_path, limit=config.tpm_limit)
    # Hash once at startup; compare_digest on the hashes keeps the lookup
    # constant-time with respect to the key bytes.
    tenants = {tenant_id(key): name for key, name in config.tenants.items()}

    def authenticate(request):
        token = _bearer(request)
        if not token:
            return None
        digest = tenant_id(token)
        for known, name in tenants.items():
            if hmac.compare_digest(known, digest):
                return digest, name
        return None

    async def chat_completions(request: Request):
        request_id = _new_id()
        started = time.monotonic()
        tenant = authenticate(request)
        if tenant is None:
            return error_response("authentication_error", request_id,
                                  headers={"www-authenticate": "Bearer"})
        tenant_key, tenant_name = tenant

        try:
            body = json.loads(await request.body())
        except ValueError:
            return error_response("invalid_request", request_id, detail="body is not valid JSON")
        problem = _validate(body)
        if problem:
            return error_response("invalid_request", request_id, detail=problem)

        stream = bool(body.get("stream"))
        if body.get("max_tokens") is None and body.get("max_completion_tokens") is None:
            # Without a ceiling the reservation would have to guess at the
            # completion size. With one it's an upper bound.
            body["max_tokens"] = DEFAULT_MAX_TOKENS
        completion_cap = body.get("max_completion_tokens") or body["max_tokens"]
        prompt_estimate = estimate_prompt_tokens(body["messages"])
        cost = prompt_estimate + completion_cap

        try:
            reservation = await limiter.reserve(tenant_key, cost)
        except TooLargeForBudget as exc:
            return error_response("request_too_large", request_id,
                                  estimated_tokens=exc.tokens, limit_tokens=exc.limit)
        except RateLimited as exc:
            log.info("rate limited", request_id=request_id, tenant=tenant_name,
                     used=exc.used, requested=cost, retry_after=round(exc.retry_after, 2))
            wait = max(1, math.ceil(exc.retry_after))
            return error_response(
                "rate_limited", request_id,
                headers={"retry-after": str(wait), "x-ratelimit-limit-tokens": str(exc.limit),
                         "x-ratelimit-remaining-tokens": str(max(0, exc.limit - exc.used))},
                retry_after_seconds=wait)

        try:
            routed = await router.route(body, stream=stream, request_id=request_id)
        except UpstreamRejected as exc:
            await limiter.release(reservation)
            return error_response("upstream_rejected", request_id, upstream_status=exc.status,
                                  attempts=[a.public() for a in exc.attempts])
        except AllProvidersFailed as exc:
            await limiter.release(reservation)
            log.warning("all providers failed", request_id=request_id,
                        attempts=[vars(a) for a in exc.attempts])
            return error_response("providers_unavailable", request_id, headers={"retry-after": "5"},
                                  attempts=[a.public() for a in exc.attempts])
        except Exception:
            await limiter.release(reservation)
            log.exception("routing crashed", request_id=request_id)
            return error_response("internal_error", request_id)

        headers = {
            "x-request-id": request_id,
            "x-tollgate-provider": routed.provider.name,
            "x-tollgate-attempts": ",".join(f"{a.provider}:{a.outcome}" for a in routed.attempts),
            "x-ratelimit-limit-tokens": str(limiter.limit),
            "x-ratelimit-remaining-tokens": str(max(0, reservation.remaining)),
        }

        if not stream:
            out = redact_openai_completion(routed.body, config.labeled) if config.redact else routed.body
            usage = (routed.body.get("usage") or {}).get("total_tokens") or cost
            await limiter.settle(reservation, usage)
            log.info("completion", request_id=request_id, tenant=tenant_name,
                     provider=routed.provider.name, tokens=usage,
                     ms=int((time.monotonic() - started) * 1000))
            return JSONResponse(out, headers=headers)

        wants_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        codec = OpenAIChat(labeled=config.labeled, keep_usage_chunk=wants_usage)
        passthrough = not config.redact

        async def relay():
            first_byte_ms = None
            events = routed.events.__aiter__()
            try:
                while True:
                    try:
                        # Per-event idle limit. A provider that goes quiet
                        # mid-stream would otherwise hold the connection open
                        # until something upstream of us gives up.
                        async with asyncio.timeout(config.idle_timeout):
                            event = await anext(events)
                    except StopAsyncIteration:
                        break
                    except Exception as exc:
                        log.warning("stream interrupted", request_id=request_id,
                                    provider=routed.provider.name, error=type(exc).__name__)
                        for tail in codec.on_end():
                            yield tail.encode()
                        yield error_event("stream_interrupted", request_id)
                        return
                    outgoing = [event] if passthrough else codec.on_event(event)
                    for out in outgoing:
                        if first_byte_ms is None:
                            first_byte_ms = int((time.monotonic() - started) * 1000)
                        yield out.encode()
                for tail in codec.on_end():
                    yield tail.encode()
                # A comment, so no client parser ever sees it as a chunk.
                yield f": tollgate redactions={json.dumps(codec.redactions)} max_held={codec.max_held}\n\n".encode()
            finally:
                # Shielded: on client disconnect this runs inside a cancelled
                # scope, and without the shield the first await re-raises
                # before the upstream is closed or the budget is corrected.
                with anyio.CancelScope(shield=True):
                    await routed.aclose()
                    if codec.usage and codec.usage.get("total_tokens"):
                        actual = codec.usage["total_tokens"]
                    else:
                        actual = prompt_estimate + tokens_for_chars(codec.text_out)
                    await limiter.settle(reservation, actual)
                    log.info("stream done", request_id=request_id, tenant=tenant_name,
                             provider=routed.provider.name, tokens=actual,
                             redactions=codec.redactions, max_held=codec.max_held,
                             ttfb_ms=first_byte_ms, ms=int((time.monotonic() - started) * 1000))

        return StreamingResponse(relay(), media_type="text/event-stream", headers={
            **headers,
            "cache-control": "no-cache",
            # nginx buffers proxied responses by default, which quietly turns a
            # stream into one big response at the end.
            "x-accel-buffering": "no",
        })

    async def healthz(request):
        return JSONResponse({
            "status": "ok",
            "providers": {p.name: router.breakers[p.name].state for p in router.providers},
        })

    async def usage(request):
        tenant = authenticate(request)
        if tenant is None:
            return error_response("authentication_error", _new_id(), headers={"www-authenticate": "Bearer"})
        used = await limiter.usage(tenant[0])
        return JSONResponse({"tenant": tenant[1], "window_seconds": limiter.window,
                             "limit_tokens": limiter.limit, "used_tokens": used})

    async def http_error(request, exc):
        kind = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "invalid_request")
        return error_response(kind, _new_id())

    async def crash(request, exc):
        request_id = _new_id()
        log.exception("unhandled error", request_id=request_id, path=request.url.path)
        return error_response("internal_error", request_id)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        yield
        await router.aclose()
        limiter.close()

    app = Starlette(
        routes=[
            Route("/v1/chat/completions", chat_completions, methods=["POST"]),
            Route("/v1/usage", usage, methods=["GET"]),
            Route("/healthz", healthz, methods=["GET"]),
        ],
        exception_handlers={HTTPException: http_error, Exception: crash},
        lifespan=lifespan,
    )
    app.state.router = router
    app.state.limiter = limiter
    return app
