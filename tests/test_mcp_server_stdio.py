"""Task 1 over a real stdio pipe.

stdout purity is a property of the process, not of any function in it, so
these spawn the server as a subprocess. TOLLGATE_CHAOS_STDOUT=1 makes every
handler try to write to stdout three different ways; the tests pass only if
none of it reaches the wire.
"""

import json
import os
import subprocess
import sys

import pytest
from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPError

pytestmark = pytest.mark.slow

CHAOS_ENV = {**os.environ, "TOLLGATE_CHAOS_STDOUT": "1", "TOLLGATE_LOG_LEVEL": "DEBUG"}
SERVER = [sys.executable, "-m", "tollgate.mcp_server"]

INIT = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                   "clientInfo": {"name": "raw-test", "version": "0"}}}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def run_raw(lines, env=CHAOS_ENV):
    """Write lines to the server's stdin, return (stdout lines, stderr text)."""
    payload = "".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines)
    # Hold stdin open briefly after the last line: on EOF the SDK shuts down and
    # can drop replies still being written, which would look like a missing response.
    script = (
        "import sys,time,subprocess;"
        "p=subprocess.Popen(sys.argv[1:],stdin=subprocess.PIPE);"
        "p.stdin.write(sys.stdin.buffer.read());p.stdin.flush();time.sleep(1.5);"
        "p.stdin.close();sys.exit(p.wait())"
    )
    proc = subprocess.run([sys.executable, "-c", script, *SERVER], input=payload.encode(),
                          capture_output=True, env=env, timeout=30)
    return proc.stdout.decode().splitlines(), proc.stderr.decode()


def by_id(stdout_lines):
    return {msg.get("id"): msg for msg in map(json.loads, stdout_lines) if "id" in msg}


@pytest.fixture(scope="module")
def chaos_session():
    lines = [
        INIT, INITIALIZED,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "get_customer_record", "arguments": {"customer_id": "CUST-10001"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "get_customer_record", "arguments": {"customer_id": "CUST-1"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "trigger_refund",
                    "arguments": {"customer_id": "CUST-10001", "amount": "12", "reason": "short"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "no_such_tool"}},
        {"jsonrpc": "2.0", "id": 6, "method": "resources/teleport"},
        "this is not json",
        '{"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {',
        {"jsonrpc": "2.0", "id": 8, "method": 42},
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"arguments": {}}},
        {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
         "params": {"name": "trigger_refund",
                    "arguments": {"customer_id": "CUST-10002", "amount": 25, "reason": "late delivery credit"}}},
    ]
    return run_raw(lines)


def test_every_stdout_line_is_jsonrpc(chaos_session):
    stdout, _ = chaos_session
    assert stdout, "server produced no output"
    for line in stdout:
        msg = json.loads(line)  # raises on any stray non-JSON write
        assert msg["jsonrpc"] == "2.0"
        assert ("result" in msg) ^ ("error" in msg) or "method" in msg


def test_chaos_writes_went_to_stderr(chaos_session):
    stdout, stderr = chaos_session
    for marker in ("chaos: print()", "chaos: os.write(1)", "chaos: child process"):
        assert marker in stderr
        assert not any(marker in line for line in stdout)


def test_logs_are_json_on_stderr(chaos_session):
    _, stderr = chaos_session
    logs = [json.loads(line) for line in stderr.splitlines() if line.startswith("{")]
    assert any(entry["msg"] == "ready" for entry in logs)
    assert any(entry["msg"] == "rejected arguments" for entry in logs)


def test_error_codes_on_the_wire(chaos_session):
    replies = by_id(chaos_session[0])
    assert "result" in replies[2]
    assert replies[3]["error"]["code"] == -32602
    assert replies[4]["error"]["code"] == -32602
    assert {v["field"] for v in replies[4]["error"]["data"]["violations"]} == {"amount", "reason"}
    assert replies[5]["error"]["code"] == -32602   # unknown tool
    assert replies[6]["error"]["code"] == -32601   # unknown method
    assert replies[8]["error"]["code"] == -32600   # valid JSON, not a request; id recovered
    assert replies[9]["error"]["code"] == -32602   # tools/call without a name
    assert replies[10]["result"]["isError"] is False


def test_unparseable_lines_get_a_parse_error(chaos_session):
    stdout, _ = chaos_session
    nulls = [m for m in map(json.loads, stdout) if m.get("id") is None and "error" in m]
    assert [m["error"]["code"] for m in nulls] == [-32700, -32700]


def test_server_keeps_serving_after_garbage(chaos_session):
    # id 10 comes after three malformed lines and still gets a real answer.
    assert by_id(chaos_session[0])[10]["result"]["structuredContent"]["status"] == "issued"


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_official_client_with_chaos_on(mode):
    params = StdioServerParameters(command=SERVER[0], args=SERVER[1:], env=CHAOS_ENV)
    async with Client(params, mode=mode) as client:
        tools = await client.list_tools()
        assert [t.name for t in tools.tools] == ["get_customer_record", "trigger_refund"]

        record = await client.call_tool("get_customer_record", {"customer_id": "CUST-10002"})
        assert record.structured_content["email"] == "marcus.chen@example.org"

        with pytest.raises(MCPError) as info:
            await client.call_tool("trigger_refund", {"customer_id": "CUST-10002", "amount": -3,
                                                      "reason": "please refund"})
        assert info.value.error.code == -32602
