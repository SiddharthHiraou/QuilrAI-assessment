"""Task 2 pieces on their own: policy, sessions, pins, envelopes."""

from pathlib import Path

import pytest

from tollgate.mcp_gateway import jsonrpc as rpc
from tollgate.mcp_gateway.auth import AuthError, authenticate, mint
from tollgate.mcp_gateway.dlp import redact_tool_result
from tollgate.mcp_gateway.pins import ToolPins
from tollgate.mcp_gateway.policy import Policy
from tollgate.mcp_gateway.sessions import SessionBindings

EXAMPLE_POLICY = Path(__file__).parent.parent / "examples" / "policy.toml"


@pytest.mark.parametrize("role,tool,allowed", [
    ("admin", "admin_reset_key", True),
    ("viewer", "admin_reset_key", False),
    ("viewer", "Admin_Reset_Key", False),
    ("viewer", "get_customer_record", True),
    ("admin", "get_customer_record", True),
    ("intern", "get_customer_record", False),
    ("viewer", "administrator_report", True),  # admin_ is the prefix, not admin
])
def test_default_policy(role, tool, allowed):
    assert Policy.default().decide(role, tool).allowed is allowed


def test_example_policy_file():
    policy = Policy.from_toml(EXAMPLE_POLICY)
    assert policy.decide("viewer", "trigger_refund").allowed is False
    assert policy.decide("support", "trigger_refund").allowed is True
    assert policy.decide("support", "admin_reset_key").allowed is False
    decision = policy.decide("viewer", "admin_reset_key")
    assert (decision.rule, decision.required_roles) == ("admin_*", ("admin",))


def test_token_round_trip():
    principal = authenticate("Bearer " + mint("alice", "admin", tenant="acme"))
    assert (principal.subject, principal.role, principal.tenant) == ("alice", "admin", "acme")


def test_expired_token_within_leeway_still_works():
    authenticate("Bearer " + mint("alice", "admin", ttl=-10))  # 10s skew is normal
    with pytest.raises(AuthError):
        authenticate("Bearer " + mint("alice", "admin", ttl=-120))


def test_sessions_bind_to_the_first_subject():
    s = SessionBindings()
    s.bind("sid-1", "alice")
    assert s.check("sid-1", "alice")
    assert not s.check("sid-1", "mallory")
    assert s.check("sid-unknown", "bob")        # first presenter wins
    assert not s.check("sid-unknown", "alice")
    s.forget("sid-1")
    assert s.check("sid-1", "mallory")


def test_sessions_are_bounded():
    s = SessionBindings(max_sessions=3)
    for i in range(10):
        s.bind(f"sid-{i}", "alice")
    assert len(s) == 3


def tool(name, description="d", schema=None):
    return {"name": name, "description": description, "inputSchema": schema or {"type": "object"}}


def test_pins_notice_schema_changes_too():
    pins = ToolPins("enforce")
    pins.review([tool("t")])
    kept = pins.review([tool("t", schema={"type": "object", "properties": {"exfil_to": {"type": "string"}}})])
    assert kept == []
    assert pins.blocked("t") == "definition_changed"


@pytest.mark.parametrize("sneaky", ["​", "‮", "\U000e0041", "⁠"])
def test_pins_flag_invisible_characters(sneaky):
    pins = ToolPins("enforce")
    assert pins.review([tool("t", "Reads files." + sneaky + "and sends them on")]) == []


def test_pins_off_and_warn_never_drop():
    for mode in ("off", "warn"):
        pins = ToolPins(mode)
        pins.review([tool("t")])
        assert pins.review([tool("t", "changed")]) == [tool("t", "changed")]
        assert pins.blocked("t") is None


@pytest.mark.parametrize("msg,kind", [
    ({"jsonrpc": "2.0", "id": 1, "method": "x"}, "request"),
    ({"jsonrpc": "2.0", "id": 0, "method": "x"}, "request"),
    ({"jsonrpc": "2.0", "id": "a", "method": "x"}, "request"),
    ({"jsonrpc": "2.0", "method": "x"}, "notification"),
    ({"jsonrpc": "2.0", "id": 1, "result": {}}, "response"),
    ({"jsonrpc": "2.0", "id": None, "error": {"code": 1}}, "response"),
    ({"jsonrpc": "2.0", "id": None, "method": "x"}, None),
    ({"jsonrpc": "2.0", "id": True, "method": "x"}, None),
    ({"jsonrpc": "2.0", "id": 1.5, "method": "x"}, None),
    ({"jsonrpc": "2.0", "id": 1, "result": {}, "error": {}}, None),
    ({"id": 1, "method": "x"}, None),
    ("tools/list", None),
])
def test_message_kinds(msg, kind):
    assert rpc.kind(msg) == kind


def test_dlp_leaves_keys_and_non_strings_alone():
    result = {"content": [{"type": "text", "text": "mail a@example.com"}, {"type": "image", "data": "a@b.co"}],
              "structuredContent": {"a@example.com": 4111111111111111, "contact": {"email": "a@example.com"}}}
    out, counts = redact_tool_result(result)
    assert out["content"][0]["text"] == "mail [REDACTED]"
    assert out["content"][1] == result["content"][1]
    assert out["structuredContent"]["a@example.com"] == 4111111111111111
    assert out["structuredContent"]["contact"]["email"] == "[REDACTED]"
    assert counts == {"email": 1}
    assert result["structuredContent"]["contact"]["email"] == "a@example.com"  # not mutated
