"""Primary/secondary routing with failover.

Fail over on 429, on the 3000ms deadline, on 5xx and on connection errors.
Don't fail over on 400/413/422: the request itself is wrong and the backup
would reject it too, after making the caller wait twice as long. 401/403/404
from a provider do fail over, because they mean our credentials or model name
for that provider are wrong, which is our problem and not the caller's.

For streaming requests the deadline covers time to the first event. Once a
token has been relayed to the client there's no failing over without sending
it duplicate or contradictory text, so after that point a failure ends the
stream with an error event instead.
"""

import asyncio
import json
import time
from dataclasses import dataclass, field

import httpx2

from tollgate import sse
from tollgate.log import Log

log = Log("router")

FAILOVER_STATUSES = {401, 403, 404, 408, 409, 425, 429}


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str | None = None
    model: str | None = None  # replaces the client's model when set
    timeout: float = 3.0


class Breaker:
    """Skip a provider that keeps failing, instead of paying its timeout every time.

    After `threshold` consecutive failures it opens for `cooldown` seconds,
    then lets exactly one request through as a probe. A 429 with Retry-After
    opens it for that long straight away: the provider has told us when to
    come back, so asking sooner just collects more 429s.
    """

    def __init__(self, threshold=5, cooldown=15.0, clock=time.monotonic):
        self.threshold = threshold
        self.cooldown = cooldown
        self.clock = clock
        self.failures = 0
        self.open_until = 0.0
        self.probing = False

    @property
    def state(self):
        if self.open_until == 0.0:
            return "closed"
        return "open" if self.clock() < self.open_until else "half_open"

    def allow(self):
        state = self.state
        if state == "closed":
            return True
        if state == "half_open" and not self.probing:
            self.probing = True
            return True
        return False

    def success(self):
        self.failures = 0
        self.open_until = 0.0
        self.probing = False

    def failure(self, open_for=None):
        self.failures += 1
        self.probing = False
        if open_for is not None:
            self.open_until = self.clock() + open_for
        elif self.failures >= self.threshold or self.state == "half_open":
            self.open_until = self.clock() + self.cooldown


@dataclass
class Attempt:
    provider: str
    # ok, rate_limited, timeout, server_error, connection_error, rejected,
    # misconfigured, circuit_open (skipped without trying)
    outcome: str
    status: int | None = None
    ms: int = 0

    def public(self):
        return {"provider": self.provider, "outcome": self.outcome}


class AllProvidersFailed(Exception):
    def __init__(self, attempts):
        super().__init__("all providers failed")
        self.attempts = attempts


class UpstreamRejected(Exception):
    def __init__(self, status, attempts):
        super().__init__(f"provider rejected the request ({status})")
        self.status = status
        self.attempts = attempts


@dataclass
class Routed:
    provider: Provider
    attempts: list
    body: dict | None = None            # non-streaming result
    events: object = None               # async iterator of sse.Event, streaming
    _response: object = field(default=None, repr=False)

    async def aclose(self):
        if self._response is not None:
            await self._response.aclose()


def _retry_after(response):
    value = response.headers.get("retry-after")
    try:
        return min(max(float(value), 1.0), 300.0) if value else None
    except ValueError:
        return None


class Router:
    def __init__(self, providers, client=None, breaker_factory=Breaker):
        if not providers:
            raise ValueError("need at least one provider")
        self.providers = providers
        self.breakers = {p.name: breaker_factory() for p in providers}
        self._client = client or httpx2.AsyncClient(
            timeout=httpx2.Timeout(connect=5.0, read=None, write=10.0, pool=5.0))

    async def aclose(self):
        await self._client.aclose()

    def _candidates(self):
        allowed = [p for p in self.providers if self.breakers[p.name].allow()]
        # Everything open: try them all anyway. Refusing outright is strictly
        # worse than paying a timeout that might succeed.
        if not allowed:
            return list(self.providers), []
        return allowed, [p for p in self.providers if p not in allowed]

    def _request(self, provider, body, stream):
        payload = dict(body)
        if provider.model:
            payload["model"] = provider.model
        if stream:
            payload["stream"] = True
            # Ask for usage on the final chunk so the limiter can settle on real
            # numbers. The gateway strips that chunk if the client didn't ask.
            payload["stream_options"] = {**(body.get("stream_options") or {}), "include_usage": True}
        headers = {"content-type": "application/json"}
        if provider.api_key:
            headers["authorization"] = "Bearer " + provider.api_key
        return self._client.build_request(
            "POST", provider.base_url.rstrip("/") + "/chat/completions",
            content=json.dumps(payload).encode(), headers=headers)

    async def route(self, body, stream=False, request_id="-"):
        candidates, skipped = self._candidates()
        attempts = [Attempt(p.name, "circuit_open") for p in skipped]
        for provider in candidates:
            breaker = self.breakers[provider.name]
            started = time.monotonic()
            response = None
            try:
                # One authority on the deadline. httpx's own read timeout is
                # off so the two can't race to name the same failure differently.
                async with asyncio.timeout(provider.timeout):
                    response = await self._client.send(self._request(provider, body, stream), stream=stream)
                    if response.status_code != 200:
                        await response.aread()
                    elif stream:
                        lines, first = await self._first_event(response)
                    else:
                        parsed = response.json()
            except TimeoutError:
                outcome, status = "timeout", None
            except httpx2.TransportError:
                outcome, status = "connection_error", None
            except ValueError:
                # A 200 whose body isn't JSON, or a stream that closed before
                # its first event. Either way not usable, so treat it as a 5xx.
                outcome, status = "server_error", None
            else:
                status = response.status_code
                if status == 200:
                    outcome = "ok"
                elif status >= 500:
                    outcome = "server_error"
                elif status == 429:
                    outcome = "rate_limited"
                elif status in FAILOVER_STATUSES:
                    outcome = "misconfigured" if status in (401, 403, 404) else "server_error"
                else:
                    outcome = "rejected"

            attempt = Attempt(provider.name, outcome, status, int((time.monotonic() - started) * 1000))
            attempts.append(attempt)
            log.info("provider attempt", request_id=request_id, provider=provider.name,
                     outcome=outcome, status=status, ms=attempt.ms)

            if outcome == "ok":
                breaker.success()
                if stream:
                    return Routed(provider, attempts, events=self._rest(first, lines), _response=response)
                return Routed(provider, attempts, body=parsed)

            if response is not None:
                if status is not None:
                    # Upstream detail goes to our log and nowhere else.
                    log.warning("provider error body", request_id=request_id, provider=provider.name,
                                status=status, body=_snippet(response))
                await response.aclose()

            if outcome == "rejected":
                breaker.success()  # the provider is healthy, the request isn't
                raise UpstreamRejected(status, attempts)
            if outcome == "misconfigured":
                log.error("provider credentials or model look wrong", provider=provider.name, status=status)
            breaker.failure(open_for=_retry_after(response) if outcome == "rate_limited" else None)

        raise AllProvidersFailed(attempts)

    async def _first_event(self, response):
        lines = response.aiter_lines()
        parser = sse.Parser()
        async for line in lines:
            event = parser.feed(line)
            if event is not None and event.comment is None:
                return lines, event
        raise ValueError("stream ended before the first event")

    async def _rest(self, first, lines):
        yield first
        async for event in sse.aiter_events(lines):
            yield event


def _snippet(response):
    try:
        return response.text[:500]
    except Exception:
        return "<unread>"
