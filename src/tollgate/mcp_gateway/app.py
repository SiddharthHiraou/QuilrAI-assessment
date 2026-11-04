"""MCP security gateway: a Streamable HTTP reverse proxy with per-tool authz.

    agent --Bearer JWT--> [gateway] --> downstream MCP server
                             |
                             +- 401 if the token doesn't verify
                             +- session id must belong to this caller
                             +- tools/list            forward, pin definitions
                             +- tools/call admin_*    role check; deny = -32001
                             |                        and nothing is forwarded
                             +- tools/call results    PII redacted by role
                             +- everything else       forward as-is

Speaks both protocol eras the SDK does: the 2025 handshake (initialize,
Mcp-Session-Id, SSE replies, GET stream, DELETE) and 2026-07-28 stateless
requests with Mcp-Method / Mcp-Name routing headers. Replies stream through
event by event; the gateway never waits for a whole SSE response.
"""

import contextlib
import json
import os
import uuid
from urllib.parse import urlsplit

import httpx2
from mcp.shared.inbound import ERROR_CODE_HTTP_STATUS, decode_header_value
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from tollgate import sse
from tollgate.log import Log
from tollgate.mcp_gateway import jsonrpc as rpc
from tollgate.mcp_gateway.auth import AuthError, authenticate
from tollgate.mcp_gateway.dlp import redact_tool_result
from tollgate.mcp_gateway.pins import ToolPins
from tollgate.mcp_gateway.policy import Policy
from tollgate.mcp_gateway.sessions import SessionBindings

log = Log("mcp_gateway")

MAX_BODY = 4 * 1024 * 1024

# Request headers that mean something to an MCP server. Everything else the
# client sent stays here: its Authorization above all, but also cookies,
# Origin, and any X-Tollgate-* it might try to forge.
FORWARD_REQUEST = {"accept", "content-type", "mcp-session-id", "mcp-protocol-version", "mcp-method",
                   "mcp-name", "last-event-id", "user-agent"}
FORWARD_RESPONSE = {"content-type", "mcp-session-id", "mcp-protocol-version", "cache-control"}


def _status_for(code):
    return ERROR_CODE_HTTP_STATUS.get(code, 200)


def _rpc_error(msg_id, code, message, data=None, headers=None):
    return JSONResponse(rpc.error(msg_id, code, message, data), status_code=_status_for(code), headers=headers)


class _Reject(Exception):
    def __init__(self, response):
        self.response = response


class Gateway:
    def __init__(self, downstream_url, policy=None, pins=None, client=None, redact_roles=("viewer",),
                 filter_tools_list=False, downstream_token=None, allowed_origins=None, jwt_key=None):
        self.url = downstream_url
        self.policy = policy or Policy.default()
        self.pins = pins or ToolPins("warn")
        self.client = client or httpx2.AsyncClient(
            timeout=httpx2.Timeout(connect=5.0, read=None, write=30.0, pool=5.0))
        self.redact_roles = frozenset(redact_roles)
        self.filter_tools_list = filter_tools_list
        self.downstream_token = downstream_token
        self.allowed_origins = set(allowed_origins or ())
        self.jwt_key = jwt_key
        self.sessions = SessionBindings()
        self.stats = {"allowed": 0, "denied": 0, "forwarded": 0, "redactions": 0, "auth_failures": 0}

    # --- edges -------------------------------------------------------------

    def _principal(self, request, ctx):
        try:
            principal = authenticate(request.headers.get("authorization"), self.jwt_key)
        except AuthError as exc:
            self.stats["auth_failures"] += 1
            log.info("auth failed", **ctx, reason=exc.description)
            body = {"error": exc.error or "invalid_request", "error_description": exc.description}
            raise _Reject(JSONResponse(body, status_code=401,
                                       headers={"www-authenticate": exc.www_authenticate()})) from None
        ctx.update(subject=principal.subject, role=principal.role)
        return principal

    def _check_origin(self, request, ctx):
        # Browsers send Origin; agents and SDK clients don't. A page on some
        # other site that resolves to localhost (DNS rebinding) is exactly the
        # case this blocks.
        origin = request.headers.get("origin")
        if origin is None or origin in self.allowed_origins:
            return
        # Compare the parsed host, not a string prefix: "http://localhost.evil.com"
        # starts with "http://localhost" too.
        if not self.allowed_origins and urlsplit(origin).hostname in ("localhost", "127.0.0.1", "::1"):
            return
        log.warning("origin refused", **ctx, origin=origin)
        raise _Reject(JSONResponse({"error": "forbidden_origin"}, status_code=403))

    def _check_session(self, request, principal, ctx):
        sid = request.headers.get("mcp-session-id")
        if sid and not self.sessions.check(sid, principal.subject):
            log.warning("session belongs to another subject", **ctx, decision="deny", rule="session-binding")
            self.stats["denied"] += 1
            # Same answer as an unknown session, so this doesn't confirm the id exists.
            raise _Reject(JSONResponse(rpc.error(None, rpc.INVALID_REQUEST, "Session not found"),
                                       status_code=404))
        return sid

    async def _body(self, request):
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY:
                raise _Reject(JSONResponse(rpc.error(None, rpc.INVALID_REQUEST, "Request body too large"),
                                           status_code=413))
            chunks.append(chunk)
        return b"".join(chunks)

    def _upstream_headers(self, request, principal, ctx, routing=True):
        out = {}
        for key, value in request.headers.items():
            key = key.lower()
            if key in FORWARD_REQUEST or key.startswith("mcp-param-"):
                if routing or key not in ("mcp-method", "mcp-name"):
                    out[key] = value
        out["x-tollgate-subject"] = principal.subject
        out["x-tollgate-role"] = principal.role
        out["x-request-id"] = ctx["request_id"]
        if self.downstream_token:
            out["authorization"] = "Bearer " + self.downstream_token
        return out

    def _downstream_headers(self, response, ctx):
        out = {k: v for k, v in response.headers.items() if k.lower() in FORWARD_RESPONSE}
        out["x-request-id"] = ctx["request_id"]
        return out

    # --- policy --------------------------------------------------------------

    def check(self, msg, headers, principal, ctx):
        """None to forward, or the JSON-RPC error to answer with instead."""
        msg_id, method = rpc.message_id(msg), msg["method"]

        # 2026-07-28 clients mirror the method and tool name into headers so
        # intermediaries can route without parsing JSON. If anything in front
        # of us trusts those headers, a request whose headers say
        # get_customer_record while its body says admin_reset_key is a bypass.
        # The body is what runs, so the body is what policy reads, and any
        # disagreement is refused outright.
        header_method = headers.get("mcp-method")
        if header_method is not None and header_method != method:
            return self._deny(ctx, msg_id, rpc.HEADER_MISMATCH,
                              "Mcp-Method header does not match the request body", rule="header-mismatch")
        if method != "tools/call":
            return None

        params = msg.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        if not isinstance(name, str) or not rpc.TOOL_NAME.match(name):
            # Can't evaluate policy, so it doesn't run. "No name, so no admin_
            # prefix, so allow" is the bypass this exists to close.
            return self._deny(ctx, msg_id, rpc.INVALID_PARAMS,
                              "tools/call needs params.name matching [A-Za-z0-9_.-/]{1,128}", rule="malformed")
        header_name = headers.get("mcp-name")
        if header_name is not None and decode_header_value(header_name) != name:
            return self._deny(ctx, msg_id, rpc.HEADER_MISMATCH,
                              "Mcp-Name header does not match params.name", tool=name, rule="header-mismatch")

        drift = self.pins.blocked(name)
        if drift:
            return self._deny(ctx, msg_id, rpc.UNAUTHORIZED_TOOL_CALL, "Unauthorized Tool Call",
                              data={"tool": name, "reason": drift, "request_id": ctx["request_id"]},
                              tool=name, rule="tool-pin")

        decision = self.policy.decide(principal.role, name)
        if not decision.allowed:
            return self._deny(ctx, msg_id, rpc.UNAUTHORIZED_TOOL_CALL, "Unauthorized Tool Call",
                              data={"tool": name, "required_roles": list(decision.required_roles),
                                    "your_role": principal.role, "request_id": ctx["request_id"]},
                              tool=name, rule=decision.rule)
        self.stats["allowed"] += 1
        log.info("tool call allowed", **ctx, tool=name, decision="allow", rule=decision.rule)
        return None

    def _deny(self, ctx, msg_id, code, message, data=None, **audit):
        self.stats["denied"] += 1
        log.warning("request denied", **ctx, decision="deny", code=code, **audit)
        if data is None:
            data = {"request_id": ctx["request_id"]}
        return rpc.error(msg_id, code, message, data)

    # --- what comes back -------------------------------------------------------

    def _hook(self, msg, principal, ctx):
        """Post-processing for the reply to this request, if it needs any."""
        method = msg.get("method")
        if method == "tools/list":
            return lambda reply: self._review_tools(reply, principal)
        if method == "tools/call" and principal.role in self.redact_roles:
            name = msg["params"]["name"]
            return lambda reply: self._redact_result(reply, ctx, name)
        return None

    def _review_tools(self, reply, principal):
        result = reply.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            return reply
        tools = self.pins.review(result["tools"])
        if self.filter_tools_list:
            tools = [t for t in tools if not isinstance(t, dict) or not isinstance(t.get("name"), str)
                     or self.policy.decide(principal.role, t["name"]).allowed]
        if len(tools) == len(result["tools"]):
            return reply
        return {**reply, "result": {**result, "tools": tools}}

    def _redact_result(self, reply, ctx, tool):
        result = reply.get("result")
        if not isinstance(result, dict):
            return reply
        result, counts = redact_tool_result(result)
        if counts:
            self.stats["redactions"] += sum(counts.values())
            log.info("redacted tool result", **ctx, tool=tool, redactions=dict(counts))
        return {**reply, "result": result}

    # --- forwarding --------------------------------------------------------------

    async def _send(self, method, request, principal, ctx, content=None, routing=True):
        upstream = self.client.build_request(method, self.url, content=content,
                                             headers=self._upstream_headers(request, principal, ctx, routing))
        try:
            response = await self.client.send(upstream, stream=True)
        except httpx2.TransportError as exc:
            # The client learns the downstream is down. Not its address or why.
            log.error("downstream unreachable", **ctx, error=type(exc).__name__, detail=str(exc)[:200])
            raise _Reject(JSONResponse(
                rpc.error(None, rpc.DOWNSTREAM_UNAVAILABLE, "Downstream MCP server unavailable",
                          {"request_id": ctx["request_id"]}), status_code=502)) from None
        self.stats["forwarded"] += 1
        new_sid = response.headers.get("mcp-session-id")
        if new_sid and new_sid != request.headers.get("mcp-session-id"):
            self.sessions.bind(new_sid, principal.subject)
        return response

    async def _relay(self, response, msg_id, hook):
        try:
            async for event in sse.aiter_events(response.aiter_lines()):
                if hook is not None and event.data and event.event in (None, "message"):
                    try:
                        message = json.loads(event.data)
                    except ValueError:
                        message = None
                    if rpc.kind(message) == "response" and message.get("id") == msg_id:
                        event = sse.Event(data=json.dumps(hook(message)), event=event.event, id=event.id,
                                          retry=event.retry)
                yield event.encode()
        finally:
            await response.aclose()

    async def forward(self, request, raw, msg, principal, ctx):
        response = await self._send("POST", request, principal, ctx, content=raw)
        hook = self._hook(msg, principal, ctx) if rpc.kind(msg) == "request" else None
        headers = self._downstream_headers(response, ctx)
        ctype = response.headers.get("content-type", "")

        if ctype.startswith("text/event-stream"):
            headers["x-accel-buffering"] = "no"
            return StreamingResponse(self._relay(response, rpc.message_id(msg), hook),
                                     status_code=response.status_code, headers=headers)

        body = await response.aread()
        await response.aclose()
        if hook is not None and ctype.startswith("application/json") and body:
            with contextlib.suppress(ValueError):
                reply = json.loads(body)
                if rpc.kind(reply) == "response" and reply.get("id") == rpc.message_id(msg):
                    body = json.dumps(hook(reply)).encode()
        return Response(body, status_code=response.status_code, headers=headers)

    async def exchange(self, request, msg, principal, ctx):
        """Forward one message and collect its reply. Used to unpack batches."""
        response = await self._send("POST", request, principal, ctx, content=json.dumps(msg).encode(),
                                    routing=False)
        hook = self._hook(msg, principal, ctx) if rpc.kind(msg) == "request" else None
        msg_id = rpc.message_id(msg)
        try:
            if rpc.kind(msg) != "request":
                return None
            if response.headers.get("content-type", "").startswith("text/event-stream"):
                async for event in sse.aiter_events(response.aiter_lines()):
                    with contextlib.suppress(ValueError):
                        reply = json.loads(event.data)
                        if rpc.kind(reply) == "response" and reply.get("id") == msg_id:
                            return hook(reply) if hook else reply
                return rpc.error(msg_id, rpc.INTERNAL_ERROR, "Downstream closed the stream without replying")
            reply = json.loads(await response.aread())
            return hook(reply) if hook and rpc.kind(reply) == "response" else reply
        except ValueError:
            return rpc.error(msg_id, rpc.INTERNAL_ERROR, "Downstream sent an unreadable reply")
        finally:
            await response.aclose()

    # --- routes --------------------------------------------------------------------

    async def post(self, request: Request):
        ctx = {"request_id": uuid.uuid4().hex[:12]}
        try:
            self._check_origin(request, ctx)
            principal = self._principal(request, ctx)
            self._check_session(request, principal, ctx)
            raw = await self._body(request)
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                return _rpc_error(None, rpc.PARSE_ERROR, "Parse error")

            if isinstance(payload, list):
                return await self.batch(request, payload, principal, ctx)

            kind = rpc.kind(payload)
            if kind is None:
                return _rpc_error(rpc.message_id(payload), rpc.INVALID_REQUEST, "Invalid Request")
            if kind in ("request", "notification"):
                # Notifications too. tools/call isn't a valid notification, but
                # "it has no id so it can't be a call" is a guess about what the
                # downstream will do with it, and a gateway shouldn't guess.
                denial = self.check(payload, request.headers, principal, ctx)
                if denial is not None and kind == "notification":
                    return Response(status_code=202)  # nothing to answer, nothing forwarded
                if denial is not None:
                    # Answered here. The downstream never sees this request.
                    return JSONResponse(denial, status_code=_status_for(denial["error"]["code"]))
            return await self.forward(request, raw, payload, principal, ctx)
        except _Reject as exc:
            return exc.response

    async def batch(self, request, messages, principal, ctx):
        # JSON-RPC batches were dropped from MCP in 2025-06-18 and current
        # servers (including the SDK's) reject them. Clients still send them
        # sometimes, so: every element gets its own policy decision (a batch
        # can't be used to smuggle an admin call past the check), allowed
        # elements go downstream one at a time, replies come back in order.
        if not messages:
            return _rpc_error(None, rpc.INVALID_REQUEST, "Empty batch")
        if "mcp-method" in request.headers or "mcp-name" in request.headers:
            return _rpc_error(None, rpc.HEADER_MISMATCH, "Routing headers can't describe a batch")
        replies = []
        for msg in messages:
            kind = rpc.kind(msg)
            if kind is None:
                replies.append(rpc.error(rpc.message_id(msg), rpc.INVALID_REQUEST, "Invalid Request"))
                continue
            if kind in ("request", "notification"):
                denial = self.check(msg, {}, principal, ctx)
                if denial is not None:
                    if kind == "request":
                        replies.append(denial)
                    continue
            reply = await self.exchange(request, msg, principal, ctx)
            if reply is not None:
                replies.append(reply)
        if not replies:
            return Response(status_code=202)
        return JSONResponse(replies)

    async def get(self, request: Request):
        ctx = {"request_id": uuid.uuid4().hex[:12]}
        try:
            self._check_origin(request, ctx)
            principal = self._principal(request, ctx)
            self._check_session(request, principal, ctx)
            response = await self._send("GET", request, principal, ctx)
        except _Reject as exc:
            return exc.response
        headers = self._downstream_headers(response, ctx)
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            headers["x-accel-buffering"] = "no"
            return StreamingResponse(self._relay(response, None, None), status_code=response.status_code,
                                     headers=headers)
        body = await response.aread()
        await response.aclose()
        return Response(body, status_code=response.status_code, headers=headers)

    async def delete(self, request: Request):
        ctx = {"request_id": uuid.uuid4().hex[:12]}
        try:
            self._check_origin(request, ctx)
            principal = self._principal(request, ctx)
            sid = self._check_session(request, principal, ctx)
            response = await self._send("DELETE", request, principal, ctx)
        except _Reject as exc:
            return exc.response
        body = await response.aread()
        await response.aclose()
        if sid and response.status_code < 300:
            self.sessions.forget(sid)
        return Response(body, status_code=response.status_code, headers=self._downstream_headers(response, ctx))

    async def pins_view(self, request: Request):
        ctx = {"request_id": uuid.uuid4().hex[:12]}
        try:
            principal = self._principal(request, ctx)
        except _Reject as exc:
            return exc.response
        if principal.role != "admin":
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if request.method == "DELETE":
            cleared = self.pins.reset()
            log.warning("tool pins reset", **ctx, cleared=cleared)
            return JSONResponse({"cleared": cleared})
        return JSONResponse(self.pins.snapshot())

    async def healthz(self, request: Request):
        return JSONResponse({"status": "ok", "downstream": self.url, "sessions": len(self.sessions),
                             "stats": self.stats, "pins": self.pins.snapshot()["mode"]})


def gateway_from_env():
    policy_path = os.environ.get("TOLLGATE_POLICY")
    origins = [o.strip() for o in os.environ.get("TOLLGATE_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    return Gateway(
        os.environ.get("TOLLGATE_DOWNSTREAM_URL", "http://127.0.0.1:8801/mcp"),
        policy=Policy.from_toml(policy_path) if policy_path else Policy.default(),
        pins=ToolPins(os.environ.get("TOLLGATE_TOOL_PINS", "warn")),
        redact_roles=[r for r in os.environ.get("TOLLGATE_REDACT_ROLES", "viewer").split(",") if r],
        filter_tools_list=os.environ.get("TOLLGATE_FILTER_TOOLS_LIST") == "1",
        downstream_token=os.environ.get("TOLLGATE_DOWNSTREAM_TOKEN") or None,
        allowed_origins=origins,
    )


def create_app(gateway=None):
    gateway = gateway or gateway_from_env()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        log.info("ready", downstream=gateway.url, pins=gateway.pins.mode,
                 redact_roles=sorted(gateway.redact_roles), filter_tools_list=gateway.filter_tools_list)
        yield
        await gateway.client.aclose()

    app = Starlette(routes=[
        Route("/mcp", gateway.post, methods=["POST"]),
        Route("/mcp", gateway.get, methods=["GET"]),
        Route("/mcp", gateway.delete, methods=["DELETE"]),
        Route("/_tollgate/pins", gateway.pins_view, methods=["GET", "DELETE"]),
        Route("/healthz", gateway.healthz, methods=["GET"]),
    ], lifespan=lifespan)
    app.state.gateway = gateway
    return app
