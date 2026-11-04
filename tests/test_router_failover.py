"""Task 4: failover, the deadline, the breaker."""

import asyncio
import json
import time

import httpx2
import pytest

from tollgate.router.router import AllProvidersFailed, Breaker, Provider, Router, UpstreamRejected

pytestmark = pytest.mark.anyio

BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
OK = {"id": "x", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
      "usage": {"total_tokens": 5}}


def sse(*payloads):
    return "".join(f"data: {json.dumps(p)}\n\n" for p in payloads) + "data: [DONE]\n\n"


class Upstreams:
    """Two fake providers behind one MockTransport, scripted per test."""

    def __init__(self, primary, secondary=None):
        self.script = {"primary": primary, "secondary": secondary or (lambda req: httpx2.Response(200, json=OK))}
        self.calls = []
        self.cancelled = []

    async def handle(self, request):
        name = request.url.host
        self.calls.append(name)
        try:
            result = self.script[name](request)
            if asyncio.iscoroutine(result):
                result = await result
            return result
        except asyncio.CancelledError:
            self.cancelled.append(name)
            raise

    def router(self, timeout=0.3, breaker=Breaker):
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle))
        providers = [Provider("primary", "http://primary/v1", timeout=timeout),
                     Provider("secondary", "http://secondary/v1", timeout=timeout)]
        return Router(providers, client=client, breaker_factory=breaker)


def status(code, **headers):
    return lambda req: httpx2.Response(code, json={"error": {"message": "org-7f3a internals"}}, headers=headers)


async def test_primary_ok_secondary_untouched():
    up = Upstreams(lambda req: httpx2.Response(200, json=OK))
    routed = await up.router().route(BODY)
    assert routed.provider.name == "primary"
    assert up.calls == ["primary"]


@pytest.mark.parametrize("primary,outcome", [
    (status(429), "rate_limited"),
    (status(500), "server_error"),
    (status(503), "server_error"),
    (status(401), "misconfigured"),
    (status(404), "misconfigured"),
])
async def test_fails_over(primary, outcome):
    up = Upstreams(primary)
    routed = await up.router().route(BODY)
    assert routed.provider.name == "secondary"
    assert [(a.provider, a.outcome) for a in routed.attempts] == [("primary", outcome), ("secondary", "ok")]


@pytest.mark.parametrize("code", [400, 413, 422])
async def test_client_errors_do_not_fail_over(code):
    up = Upstreams(status(code))
    with pytest.raises(UpstreamRejected) as info:
        await up.router().route(BODY)
    assert info.value.status == code
    assert up.calls == ["primary"]


async def test_connection_error_fails_over():
    def refuse(req):
        raise httpx2.ConnectError("connection refused")
    routed = await Upstreams(refuse).router().route(BODY)
    assert routed.attempts[0].outcome == "connection_error"


async def test_garbage_200_fails_over():
    routed = await Upstreams(lambda req: httpx2.Response(200, content=b"<html>oops")).router().route(BODY)
    assert routed.provider.name == "secondary"


async def test_timeout_fails_over_at_the_deadline_and_cancels_the_primary():
    async def hang(req):
        await asyncio.sleep(30)
    up = Upstreams(hang)
    started = time.monotonic()
    routed = await up.router(timeout=0.25).route(BODY)
    elapsed = time.monotonic() - started
    assert routed.provider.name == "secondary"
    assert routed.attempts[0].outcome == "timeout"
    assert 0.25 <= elapsed < 0.6
    assert up.cancelled == ["primary"]  # torn down, not left running


@pytest.mark.slow
async def test_default_deadline_is_3000ms():
    async def hang(req):
        await asyncio.sleep(30)
    up = Upstreams(hang)
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(up.handle))
    router = Router([Provider("primary", "http://primary/v1"), Provider("secondary", "http://secondary/v1")],
                    client=client)
    started = time.monotonic()
    routed = await router.route(BODY)
    assert routed.provider.name == "secondary"
    assert 3.0 <= time.monotonic() - started < 3.5


async def test_slow_but_inside_the_deadline_is_not_a_timeout():
    async def slow(req):
        await asyncio.sleep(0.1)
        return httpx2.Response(200, json=OK)
    routed = await Upstreams(slow).router(timeout=0.3).route(BODY)
    assert routed.provider.name == "primary"


async def test_everything_failing():
    up = Upstreams(status(503), status(429))
    with pytest.raises(AllProvidersFailed) as info:
        await up.router().route(BODY)
    assert [a.outcome for a in info.value.attempts] == ["server_error", "rate_limited"]


# --- streaming --------------------------------------------------------------------

async def test_stream_deadline_covers_the_first_event():
    async def headers_then_nothing(req):
        async def body():
            await asyncio.sleep(30)
            yield b""
        return httpx2.Response(200, content=body(), headers={"content-type": "text/event-stream"})
    up = Upstreams(headers_then_nothing,
                   lambda req: httpx2.Response(200, text=sse({"choices": []}),
                                               headers={"content-type": "text/event-stream"}))
    routed = await up.router(timeout=0.25).route(BODY, stream=True)
    assert routed.provider.name == "secondary"
    assert routed.attempts[0].outcome == "timeout"
    events = [e async for e in routed.events]
    assert events[-1].data == "[DONE]"
    await routed.aclose()


async def test_stream_that_closes_before_any_event_fails_over():
    up = Upstreams(lambda req: httpx2.Response(200, text="", headers={"content-type": "text/event-stream"}),
                   lambda req: httpx2.Response(200, text=sse({"choices": []})))
    routed = await up.router().route(BODY, stream=True)
    assert routed.provider.name == "secondary"
    await routed.aclose()


async def test_stream_asks_for_usage_and_swaps_model():
    seen = {}

    def capture(req):
        seen.update(json.loads(req.content))
        return httpx2.Response(200, text=sse({"choices": []}))
    up = Upstreams(capture)
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(up.handle))
    router = Router([Provider("primary", "http://primary/v1", model="gpt-real")], client=client)
    routed = await router.route(BODY, stream=True)
    await routed.aclose()
    assert seen["model"] == "gpt-real"
    assert seen["stream_options"] == {"include_usage": True}


# --- breaker ----------------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


async def test_breaker_skips_a_dead_primary_then_probes():
    clock = FakeClock()
    up = Upstreams(status(503))
    router = up.router(breaker=lambda: Breaker(threshold=2, cooldown=10, clock=clock))
    await router.route(BODY)
    await router.route(BODY)
    assert router.breakers["primary"].state == "open"

    up.calls.clear()
    routed = await router.route(BODY)
    assert up.calls == ["secondary"]
    assert routed.attempts[0].outcome == "circuit_open"

    clock.t = 11
    up.script["primary"] = lambda req: httpx2.Response(200, json=OK)
    up.calls.clear()
    routed = await router.route(BODY)
    assert up.calls == ["primary"]  # the half-open probe
    assert router.breakers["primary"].state == "closed"


async def test_429_retry_after_opens_the_breaker_for_that_long():
    clock = FakeClock()
    up = Upstreams(status(429, **{"retry-after": "20"}))
    router = up.router(breaker=lambda: Breaker(clock=clock))
    await router.route(BODY)
    clock.t = 19
    assert router.breakers["primary"].state == "open"
    clock.t = 21
    assert router.breakers["primary"].state == "half_open"


def test_half_open_allows_exactly_one_probe():
    clock = FakeClock()
    b = Breaker(threshold=1, cooldown=5, clock=clock)
    b.failure()
    clock.t = 6
    assert b.allow() is True
    assert b.allow() is False
    b.failure()
    assert b.state == "open"
