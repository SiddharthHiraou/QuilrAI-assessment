"""A fake OpenAI-compatible provider with failure modes on demand.

Modes: ok, rate_limit, error, slow, stall. Set the default with --mode, flip it
at runtime with POST /_mock {"mode": ...} (that's what the demo does), or per
request with an x-mock-mode header (that's what the tests do).

The error bodies are full of things that must never reach a gateway client:
org ids, internal hostnames, a key. The sanitisation tests look for them.
"""

import asyncio
import json
import random
import time

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route

PII_REPLY = (
    "Sure. The customer is Priya Raman, email priya.raman@example.com, phone (415) 555-0132. "
    "The card on file is 4111 1111 1111 1111 and the SSN on the account is 123-45-6789. "
    "Their old card, 4532-0151-1283-0366, was replaced in March. Anything else?"
)

LEAKY_429 = {"error": {"message": "Rate limit reached for org-7f3a2b9c in us-east-1c (shard tpm-04)",
                       "type": "tokens", "internal_request_id": "req_83hd72nd"}}
LEAKY_500 = (
    "Traceback (most recent call last):\n  File \"/srv/inference/router.py\", line 212\n"
    "InternalError: upstream=10.12.4.7:9000 api_key=sk-live-SECRETSECRETSECRET1234567890 "
    "org_id=org-7f3a2b9c"
)


def reply_for(messages, name):
    last = ""
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            content = m.get("content")
            last = content if isinstance(content, str) else json.dumps(content)
            break
    if any(word in last.lower() for word in ("customer", "pii", "card", "details")):
        return PII_REPLY
    return f"({name}) You said: {last[:400]}"


def chunks(text, rng):
    # Small, uneven pieces, so PII routinely lands across chunk boundaries the
    # way it does with real tokenizers.
    i = 0
    while i < len(text):
        n = rng.randint(1, 6)
        yield text[i:i + n]
        i += n


def create_app(name="mock", mode="ok", delay_ms=15, seed=7):
    state = {"mode": mode, "calls": 0}

    async def completions(request: Request):
        state["calls"] += 1
        body = await request.json()
        current = request.headers.get("x-mock-mode", state["mode"])
        model = body.get("model") or name

        if current == "rate_limit":
            return JSONResponse(LEAKY_429, status_code=429, headers={"retry-after": "7"})
        if current == "error":
            return PlainTextResponse(LEAKY_500, status_code=500)
        if current == "reject":
            return JSONResponse({"error": {"message": "max_tokens is too large for org-7f3a2b9c",
                                           "type": "invalid_request_error"}}, status_code=400)
        if current == "slow":
            await asyncio.sleep(10)

        text = reply_for(body.get("messages") or [], name)
        prompt_tokens = sum(len(json.dumps(m)) for m in body.get("messages") or []) // 4
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": len(text) // 4,
                 "total_tokens": prompt_tokens + len(text) // 4}
        created = int(time.time())
        cid = f"chatcmpl-{name}-{state['calls']}"

        if not body.get("stream"):
            return JSONResponse({
                "id": cid, "object": "chat.completion", "created": created, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}],
                "usage": usage,
            })

        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        rng = random.Random(seed + state["calls"])

        async def events():
            def frame(choices, **extra):
                payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
                           "model": model, "choices": choices, **extra}
                return f"data: {json.dumps(payload)}\n\n"

            yield frame([{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}])
            for i, piece in enumerate(chunks(text, rng)):
                if current == "stall" and i == 5:
                    await asyncio.sleep(3600)
                await asyncio.sleep(delay_ms / 1000)
                yield frame([{"index": 0, "delta": {"content": piece}, "finish_reason": None}])
            yield frame([{"index": 0, "delta": {}, "finish_reason": "stop"}])
            if include_usage:
                yield frame([], usage=usage)
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    async def set_mode(request: Request):
        state["mode"] = (await request.json())["mode"]
        return JSONResponse(state)

    async def get_state(request: Request):
        return JSONResponse(state)

    return Starlette(routes=[
        Route("/v1/chat/completions", completions, methods=["POST"]),
        Route("/_mock", set_mode, methods=["POST"]),
        Route("/_mock", get_state, methods=["GET"]),
    ])
