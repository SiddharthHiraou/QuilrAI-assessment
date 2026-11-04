"""make demo: every task, end to end, narrated.

Starts the real services in this process on free ports (the stdio server is a
real subprocess) and drives them with the official MCP client and plain HTTP.
Takes about 15 seconds, most of it the 3s failover deadline doing its job.
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
import time

import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from tollgate.llm_gateway.app import create_app as llm_gateway
from tollgate.llm_gateway.config import Config
from tollgate.mcp_gateway.app import Gateway
from tollgate.mcp_gateway.app import create_app as mcp_gateway
from tollgate.mcp_gateway.auth import mint
from tollgate.mcp_server.server import build_server
from tollgate.mock_llm.app import create_app as mock_provider
from tollgate.router.router import Provider
from tollgate.testing import Recorder, serving

# The demo narrates; the services' JSON logs would drown it out.
logging.getLogger("tollgate").setLevel(logging.CRITICAL)

TTY = sys.stdout.isatty()


def c(code, text):
    return f"\033[{code}m{text}\033[0m" if TTY else text


def title(text):
    print("\n" + c("1;36", text) + "\n" + c("36", "-" * len(text)))


def step(text):
    print(c("1", "> ") + text)


def ok(text):
    print("  " + c("32", "ok ") + text)


def no(text):
    print("  " + c("33", "-- ") + text)


def detail(text):
    print("     " + c("2", text))


async def task1():
    title("Task 1  MCP server over stdio, strict validation")
    env = {**os.environ, "TOLLGATE_CHAOS_STDOUT": "1"}
    step("spawning the server with TOLLGATE_CHAOS_STDOUT=1, so every handler tries to write to stdout")
    params = StdioServerParameters(command=sys.executable, args=["-m", "tollgate.mcp_server"], env=env)
    with tempfile.TemporaryFile("w+") as stderr:
        async with Client(stdio_client(params, errlog=stderr), mode="legacy") as client:
            tools = await client.list_tools()
            ok("tools/list -> " + ", ".join(t.name for t in tools.tools))
            record = await client.call_tool("get_customer_record", {"customer_id": "CUST-10002"})
            ok(f"get_customer_record CUST-10002 -> {record.structured_content['name']}, "
               f"{record.structured_content['status']}")
            for args in ({"customer_id": "CUST-1"}, {"customer_id": "CUST-١٠٠٠٢"}, {"customer_id": "CUST-10002\n"}):
                try:
                    await client.call_tool("get_customer_record", args)
                except MCPError as e:
                    no(f"{json.dumps(args, ensure_ascii=False)} -> {e.error.code} {e.error.message}")
            try:
                await client.call_tool("trigger_refund", {"customer_id": "CUST-10002", "amount": "50",
                                                          "reason": "  short  ", "resaon": "typo"})
            except MCPError as e:
                no(f"trigger_refund with a string amount, padded reason, typo'd field -> {e.error.code}")
                for v in e.error.data["violations"]:
                    detail(f"{v['field']}: {v['issue']}")
            frozen = await client.call_tool("trigger_refund", {"customer_id": "CUST-10003", "amount": 10,
                                                               "reason": "duplicate charge in March"})
            no("refund on a frozen account -> isError result, a tool error the model can read")
            detail(frozen.content[0].text)
        stderr.seek(0)
        diverted = sum(1 for line in stderr if line.startswith("chaos:"))
    ok(f"{diverted} stray stdout writes (print, os.write(1), a child process) all landed on stderr; "
       "the client saw only JSON-RPC")


async def task2():
    title("Task 2  MCP security gateway")
    recorder = Recorder(build_server(admin_tools=True).streamable_http_app())
    with serving(recorder) as down:
        recorder.url = down + "/mcp"
        with serving(mcp_gateway(Gateway(recorder.url))) as gw:
            url = gw + "/mcp"
            for role in ("viewer", "admin"):
                step(f"official MCP client as {role!r}, through the gateway to the real billing server")
                http = httpx2.AsyncClient(headers={"authorization": "Bearer " + mint(f"{role}-1", role)})
                async with Client(streamable_http_client(url, http_client=http)) as client:
                    tools = await client.list_tools()
                    ok(f"tools/list forwarded as-is: {len(tools.tools)} tools")
                    record = await client.call_tool("get_customer_record", {"customer_id": "CUST-10001"})
                    sc = record.structured_content
                    ok(f"get_customer_record -> email {sc['email']}, phone {sc['phone']}")
                    before = len(recorder.tools_called())
                    try:
                        await client.call_tool("admin_reset_key", {"tenant": "acme"})
                        ok("admin_reset_key -> allowed, key rotated")
                    except MCPError as e:
                        no(f"admin_reset_key -> {e.error.code} {e.error.message} "
                           f"(needs {e.error.data['required_roles']}, has {e.error.data['your_role']!r})")
                        reached = recorder.tools_called()[before:]
                        detail(f"calls that reached the downstream for that attempt: {reached or 'none'}")

            step("raw HTTP: headers name a harmless tool, body calls the admin one (2026-07-28 routing headers)")
            meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientInfo": {"name": "demo", "version": "0"},
                    "io.modelcontextprotocol/clientCapabilities": {}}
            r = httpx2.post(url, headers={
                "authorization": "Bearer " + mint("a", "admin"), "accept": "application/json, text/event-stream",
                "mcp-protocol-version": "2026-07-28", "mcp-method": "tools/call", "mcp-name": "get_customer_record",
            }, json={"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                     "params": {"name": "admin_reset_key", "arguments": {"tenant": "acme"}, "_meta": meta}})
            no(f"-> HTTP {r.status_code}, {r.json()['error']['code']} {r.json()['error']['message']}")

            step("viewer replays an admin's Mcp-Session-Id")
            init = httpx2.post(url, headers={"authorization": "Bearer " + mint("alice", "admin"),
                                             "accept": "application/json, text/event-stream"},
                               json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                                   "protocolVersion": "2025-11-25", "capabilities": {},
                                   "clientInfo": {"name": "demo", "version": "0"}}})
            sid = init.headers["mcp-session-id"]
            r = httpx2.post(url, headers={"authorization": "Bearer " + mint("mallory", "viewer"),
                                          "accept": "application/json, text/event-stream",
                                          "mcp-session-id": sid, "mcp-protocol-version": "2025-11-25"},
                            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            no(f"-> HTTP {r.status_code} {r.json()['error']['message']} (session is bound to alice)")
            stats = httpx2.get(gw + "/healthz").json()["stats"]
            detail(f"gateway stats: {stats}")


async def task3_and_4():
    db = tempfile.mktemp(suffix=".sqlite3")
    with serving(mock_provider("primary", delay_ms=12)) as p1, serving(mock_provider("secondary", delay_ms=12)) as p2:
        config = Config(providers=[Provider("primary", p1 + "/v1"), Provider("secondary", p2 + "/v1")],
                        tenants={"tg-dev-acme": "acme", "tg-dev-tiny": "tiny"}, db_path=db, tpm_limit=50_000)
        with serving(llm_gateway(config)) as gw:
            key = {"authorization": "Bearer tg-dev-acme"}
            body = {"model": "any", "messages": [{"role": "user", "content": "customer details please"}],
                    "stream": True}

            title("Task 3  LLM gateway, PII redacted out of the live stream")
            step("provider streams PII in 1-6 character chunks; this is what the client receives, as it arrives")
            started = time.monotonic()
            first = None
            print("     ", end="", flush=True)
            with httpx2.stream("POST", gw + "/v1/chat/completions", json=body, headers=key, timeout=30) as r:
                for line in r.iter_lines():
                    if line.startswith("data: {"):
                        for choice in json.loads(line[6:]).get("choices", []):
                            text = (choice.get("delta") or {}).get("content") or ""
                            if text and first is None:
                                first = time.monotonic() - started
                            print(text.replace("[REDACTED]", c("1;35", "[REDACTED]")), end="", flush=True)
                    elif line.startswith(": tollgate"):
                        stats = line[2:]
            print()
            total = time.monotonic() - started
            ok(f"first token after {first * 1000:.0f} ms, stream finished after {total * 1000:.0f} ms")
            detail(stats)

            title("Task 4  token budget and failover")
            step("primary starts returning 429 (its error body is full of internal ids)")
            httpx2.post(p1 + "/_mock", json={"mode": "rate_limit"})
            r = httpx2.post(gw + "/v1/chat/completions", json={**body, "stream": False}, headers=key, timeout=30)
            ok(f"HTTP {r.status_code}, served by {r.headers['x-tollgate-provider']} "
               f"(attempts: {r.headers['x-tollgate-attempts']})")

            step("primary hangs instead (on a fresh gateway, so the breaker the 429 opened doesn't just skip it)")
            httpx2.post(p1 + "/_mock", json={"mode": "slow"})
            fresh = Config(providers=config.providers, tenants=config.tenants, db_path=db)
            with serving(llm_gateway(fresh)) as gw2:
                started = time.monotonic()
                r = httpx2.post(gw2 + "/v1/chat/completions", json={**body, "stream": False}, headers=key, timeout=30)
                ok(f"HTTP {r.status_code} after {time.monotonic() - started:.2f}s, "
                   f"attempts: {r.headers['x-tollgate-attempts']}")

                step("both providers fail; here's everything the client gets to see")
                httpx2.post(p1 + "/_mock", json={"mode": "error"})
                httpx2.post(p2 + "/_mock", json={"mode": "error"})
                r = httpx2.post(gw2 + "/v1/chat/completions", json={**body, "stream": False}, headers=key)
                no(f"HTTP {r.status_code} {r.text}")

            title("Task 4  sliding window, 50,000 tokens/minute per key")
            httpx2.post(p1 + "/_mock", json={"mode": "ok"})
            httpx2.post(p2 + "/_mock", json={"mode": "ok"})
            tiny = Config(providers=config.providers, tenants=config.tenants, db_path=db, tpm_limit=2_000)
            with serving(llm_gateway(tiny)) as gw3:
                step("a tenant with a 2,000 token budget sends long prompts until it's refused")
                prompt = "please summarise the attached notes. " * 30
                for i in range(1, 8):
                    r = httpx2.post(gw3 + "/v1/chat/completions", headers={"authorization": "Bearer tg-dev-tiny"},
                                    json={"model": "m", "messages": [{"role": "user", "content": prompt}],
                                          "max_tokens": 200})
                    if r.status_code == 200:
                        ok(f"request {i}: 200, {r.headers['x-ratelimit-remaining-tokens']} tokens left in window")
                    else:
                        no(f"request {i}: {r.status_code} {r.json()['error']['type']}, "
                           f"Retry-After {r.headers['retry-after']}s (when the oldest usage ages out)")
                        break
                r = httpx2.post(gw3 + "/v1/chat/completions", headers={"authorization": "Bearer tg-dev-acme"},
                                json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})
                ok(f"a different tenant at the same moment: {r.status_code}")
    os.unlink(db)


async def main():
    started = time.monotonic()
    await task1()
    await task2()
    await task3_and_4()
    print("\n" + c("1;32", f"done in {time.monotonic() - started:.1f}s"))


if __name__ == "__main__":
    asyncio.run(main())
