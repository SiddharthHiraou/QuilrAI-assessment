"""Tasks 3 and 4 wired together: the gateway's HTTP edges and error contract."""

import json
import tempfile

import httpx2
import pytest

from tollgate.llm_gateway.app import create_app
from tollgate.llm_gateway.config import Config
from tollgate.mock_llm.app import create_app as mock_provider
from tollgate.router.limiter import TokenLimiter
from tollgate.router.router import Provider, Router
from tollgate.testing import serving

pytestmark = pytest.mark.anyio

KEY = {"authorization": "Bearer tg-test"}
LEAKS = ("org-7f3a2b9c", "us-east-1c", "10.12.4.7", "sk-live", "Traceback", "/srv/inference", "req_83hd72nd")


def make_gateway(primary_mode="ok", secondary_mode="ok", limit=50_000, idle_timeout=30.0, timeout=3.0):
    primary = mock_provider("primary", primary_mode, delay_ms=0)
    secondary = mock_provider("secondary", secondary_mode, delay_ms=0)

    async def dispatch(request):
        app = primary if request.url.host == "primary" else secondary
        return await httpx2.ASGITransport(app=app).handle_async_request(request)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(dispatch))
    router = Router([Provider("primary", "http://primary/v1", timeout=timeout),
                     Provider("secondary", "http://secondary/v1", timeout=timeout)], client=client)
    limiter = TokenLimiter(tempfile.mktemp(suffix=".sqlite3"), limit=limit)
    config = Config(providers=router.providers, tenants={"tg-test": "acme", "tg-other": "globex"},
                    db_path=limiter.path, idle_timeout=idle_timeout)
    app = create_app(config, router=router, limiter=limiter)
    return app, limiter


def client_for(app):
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw")


def chat(content="hello", **extra):
    return {"model": "m", "messages": [{"role": "user", "content": content}], **extra}


def assert_standard_error(response, kind):
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["type"] == kind
    assert body["error"]["request_id"].startswith("req_")
    assert response.headers["x-request-id"] == body["error"]["request_id"]
    for leak in LEAKS:
        assert leak not in response.text


# --- edges -------------------------------------------------------------------------

@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer nope"}, {"authorization": "Basic dGc="},
                                     {"authorization": "tg-test"}])
async def test_auth(headers):
    app, _ = make_gateway()
    async with client_for(app) as c:
        r = await c.post("/v1/chat/completions", json=chat(), headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert_standard_error(r, "authentication_error")


@pytest.mark.parametrize("body", [
    b"{not json", b"[]", b'{"messages": []}', b'{"messages": "hi"}', b'{"messages": [{"content": "x"}]}',
    b'{"messages": [{"role": "user", "content": "x"}], "stream": "yes"}',
    b'{"messages": [{"role": "user", "content": "x"}], "max_tokens": 0}',
    b'{"messages": [{"role": "user", "content": "x"}], "max_tokens": true}',
    b'{"messages": [{"role": "user", "content": "x"}], "max_tokens": 99999999}',
])
async def test_invalid_bodies(body):
    app, _ = make_gateway()
    async with client_for(app) as c:
        r = await c.post("/v1/chat/completions", content=body, headers=KEY)
    assert r.status_code == 400
    assert_standard_error(r, "invalid_request")


async def test_unknown_route_and_method_use_the_same_shape():
    app, _ = make_gateway()
    async with client_for(app) as c:
        assert_standard_error(await c.get("/v1/engines"), "not_found")
        assert_standard_error(await c.get("/v1/chat/completions"), "method_not_allowed")


# --- rate limiting ------------------------------------------------------------------

async def test_rate_limit_returns_429_with_retry_after():
    app, _ = make_gateway(limit=1_000)
    long_prompt = "please summarise this. " * 26  # ~600 chars, ~300 real tokens with the reply
    async with client_for(app) as c:
        statuses = []
        for _ in range(6):
            r = await c.post("/v1/chat/completions", json=chat(long_prompt, max_tokens=100), headers=KEY)
            statuses.append(r.status_code)
            if r.status_code == 429:
                break
    assert statuses[:3] == [200, 200, 200]
    assert statuses[-1] == 429
    assert int(r.headers["retry-after"]) >= 1
    assert r.json()["error"]["retry_after_seconds"] == int(r.headers["retry-after"])
    assert r.headers["x-ratelimit-limit-tokens"] == "1000"
    assert_standard_error(r, "rate_limited")


async def test_reservations_settle_to_real_usage():
    # A big max_tokens is reserved up front, then corrected to what was used,
    # so the next request isn't refused over tokens nobody spent.
    app, _ = make_gateway(limit=3_000)
    async with client_for(app) as c:
        for _ in range(5):
            r = await c.post("/v1/chat/completions", json=chat(max_tokens=2_900), headers=KEY)
            assert r.status_code == 200
        usage = (await c.get("/v1/usage", headers=KEY)).json()
    assert usage["used_tokens"] < 100


async def test_other_tenants_are_unaffected():
    app, _ = make_gateway(limit=2_000)
    async with client_for(app) as c:
        await c.post("/v1/chat/completions", json=chat(max_tokens=1_990), headers=KEY)
        r = await c.post("/v1/chat/completions", json=chat(max_tokens=1_990), headers=KEY)
        assert r.status_code == 429
        r = await c.post("/v1/chat/completions", json=chat(max_tokens=1_990),
                         headers={"authorization": "Bearer tg-other"})
    assert r.status_code == 200


async def test_oversized_request_is_400_not_429():
    # Retrying can never help this one, so a Retry-After would be a lie.
    app, _ = make_gateway(limit=1_000)
    async with client_for(app) as c:
        r = await c.post("/v1/chat/completions", json=chat(max_tokens=5_000), headers=KEY)
    assert r.status_code == 400
    assert "retry-after" not in r.headers
    assert_standard_error(r, "request_too_large")


async def test_failed_requests_give_their_reservation_back():
    app, limiter = make_gateway("error", "error")
    async with client_for(app) as c:
        r = await c.post("/v1/chat/completions", json=chat(max_tokens=4_000), headers=KEY)
        assert r.status_code == 503
        usage = await c.get("/v1/usage", headers=KEY)
    assert usage.json()["used_tokens"] == 0


# --- failover and sanitisation -----------------------------------------------------

async def test_failover_is_visible_in_headers():
    app, _ = make_gateway("rate_limit")
    async with client_for(app) as c:
        r = await c.post("/v1/chat/completions", json=chat(), headers=KEY)
    assert r.status_code == 200
    assert r.headers["x-tollgate-provider"] == "secondary"
    assert r.headers["x-tollgate-attempts"] == "primary:rate_limited,secondary:ok"
    assert "(secondary)" in r.json()["choices"][0]["message"]["content"]


@pytest.mark.parametrize("modes", [("rate_limit", "error"), ("error", "error"), ("rate_limit", "rate_limit")])
async def test_all_providers_failing_is_sanitised(modes):
    app, _ = make_gateway(*modes)
    async with client_for(app) as c:
        r = await c.post("/v1/chat/completions", json=chat(), headers=KEY)
    assert r.status_code == 503
    assert_standard_error(r, "providers_unavailable")
    assert {a["provider"] for a in r.json()["error"]["attempts"]} == {"primary", "secondary"}


async def test_upstream_rejection_is_sanitised_and_not_retried():
    app, _ = make_gateway("reject")
    async with client_for(app) as c:
        r = await c.post("/v1/chat/completions", json=chat(), headers=KEY)
    assert r.status_code == 400
    assert_standard_error(r, "upstream_rejected")
    assert r.json()["error"]["attempts"] == [{"provider": "primary", "outcome": "rejected"}]


async def test_timeout_failover_end_to_end():
    app, _ = make_gateway("slow", timeout=0.3)
    async with client_for(app) as c:
        r = await c.post("/v1/chat/completions", json=chat(), headers=KEY)
    assert r.headers["x-tollgate-attempts"] == "primary:timeout,secondary:ok"


async def test_non_streaming_response_is_redacted():
    app, _ = make_gateway()
    async with client_for(app) as c:
        r = await c.post("/v1/chat/completions", json=chat("customer details"), headers=KEY)
    content = r.json()["choices"][0]["message"]["content"]
    assert "[REDACTED]" in content and "4111" not in content


# --- mid-stream failure, on real sockets ----------------------------------------------

def test_stall_after_first_token_ends_with_an_error_event():
    with serving(mock_provider("primary", "stall", delay_ms=0)) as primary:
        config = Config(providers=[Provider("primary", primary + "/v1")], tenants={"tg-test": "acme"},
                        db_path=tempfile.mktemp(suffix=".sqlite3"), idle_timeout=0.5)
        with serving(create_app(config)) as gw:
            with httpx2.stream("POST", gw + "/v1/chat/completions", headers=KEY,
                               json=chat("customer details", stream=True), timeout=10) as r:
                lines = [line for line in r.iter_lines() if line.startswith("data: ")]
    assert r.status_code == 200  # headers were already sent when it stalled
    last = json.loads(lines[-1][6:])
    assert last["error"]["type"] == "stream_interrupted"
    assert "[DONE]" not in lines[-1]


def test_client_disconnect_settles_the_budget():
    with serving(mock_provider("primary", delay_ms=30)) as primary:
        db = tempfile.mktemp(suffix=".sqlite3")
        config = Config(providers=[Provider("primary", primary + "/v1")], tenants={"tg-test": "acme"},
                        db_path=db)
        with serving(create_app(config)) as gw:
            with httpx2.stream("POST", gw + "/v1/chat/completions", headers=KEY,
                               json=chat("customer details", stream=True, max_tokens=8_000), timeout=10) as r:
                for i, _ in enumerate(r.iter_lines()):
                    if i > 6:
                        break  # walk away mid-stream
            import time
            for _ in range(50):
                used = httpx2.get(gw + "/v1/usage", headers=KEY).json()["used_tokens"]
                if used < 8_000:
                    break
                time.sleep(0.1)
    # The 8k reservation was corrected down to what was actually streamed.
    assert 0 < used < 500
