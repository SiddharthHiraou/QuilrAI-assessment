"""Task 1, in-process: validation, error mapping, business rules."""

import asyncio
import json
import math

import mcp_types as types
import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError

from tollgate.mcp_server.server import build_server
from tollgate.mcp_server.store import BillingStore

pytestmark = pytest.mark.anyio

GOOD_REASON = "customer was double charged"


@pytest.fixture
async def client():
    async with Client(build_server()) as c:
        yield c


async def call(client, name, args):
    return await client.call_tool(name, args)


async def invalid_params(client, name, args):
    with pytest.raises(MCPError) as info:
        await call(client, name, args)
    assert info.value.error.code == types.INVALID_PARAMS
    return info.value.error


# --- get_customer_record -----------------------------------------------------

async def test_get_customer_record(client):
    result = await call(client, "get_customer_record", {"customer_id": "CUST-10001"})
    assert result.is_error is False
    assert result.structured_content["name"] == "Priya Raman"
    assert json.loads(result.content[0].text) == result.structured_content


@pytest.mark.parametrize("customer_id", [
    "",
    "CUST-1234",
    "CUST-123456",
    "cust-10001",
    "CUST_10001",
    " CUST-10001",
    "CUST-10001 ",
    "CUST-10001\n",       # $ in Python's re would let this through
    "CUST-١٠٠٠١",         # Arabic-Indic digits, \d would let this through
    "CUST-1000１",         # fullwidth digit
    "CUST-10001; DROP TABLE customers",
])
async def test_bad_customer_id_strings(client, customer_id):
    err = await invalid_params(client, "get_customer_record", {"customer_id": customer_id})
    assert err.data["violations"][0]["field"] == "customer_id"


@pytest.mark.parametrize("value", [10001, None, ["CUST-10001"], {"id": "CUST-10001"}, True])
async def test_customer_id_must_be_a_string(client, value):
    await invalid_params(client, "get_customer_record", {"customer_id": value})


async def test_missing_and_extra_fields(client):
    err = await invalid_params(client, "get_customer_record", {})
    assert err.data["violations"][0]["type"] == "missing"
    err = await invalid_params(client, "get_customer_record",
                               {"customer_id": "CUST-10001", "include_ssn": True})
    assert err.data["violations"][0]["type"] == "extra_forbidden"


async def test_unknown_customer_is_a_tool_error_not_a_protocol_error(client):
    result = await call(client, "get_customer_record", {"customer_id": "CUST-99999"})
    assert result.is_error is True
    assert json.loads(result.content[0].text)["error"] == "customer_not_found"


# --- trigger_refund ----------------------------------------------------------

async def test_refund_happy_path(client):
    result = await call(client, "trigger_refund",
                        {"customer_id": "CUST-10001", "amount": 49.99, "reason": GOOD_REASON})
    assert result.is_error is False
    receipt = result.structured_content
    assert receipt["status"] == "issued"
    assert receipt["remaining_refundable_usd"] == pytest.approx(1150.01)


@pytest.mark.parametrize("amount", [50, 0.01, 10.5, 10_000])
async def test_valid_amounts(amount):
    store = BillingStore.seeded()
    store._customers["CUST-10001"] = store._customers["CUST-10001"].__class__(
        **{**vars(store._customers["CUST-10001"]), "refundable_balance_usd": 20_000.0})
    async with Client(build_server(store)) as c:
        result = await c.call_tool("trigger_refund",
                                   {"customer_id": "CUST-10001", "amount": amount, "reason": GOOD_REASON})
        assert result.is_error is False


@pytest.mark.parametrize("amount,expected_type", [
    (0, "greater_than"),
    (-5, "greater_than"),
    (-0.0, "greater_than"),
    (10_000.01, "less_than_equal"),
    (1e308, "less_than_equal"),
    ("50", "float_type"),
    ("50.00", "float_type"),
    (True, "value_error"),
    (None, "float_type"),
    ([50], "float_type"),
    (10.005, "value_error"),
    (0.001, "value_error"),
    # These don't survive JSON encoding as numbers, whichever way they arrive
    # they have to be rejected.
    (math.inf, None),
    (math.nan, None),
])
async def test_bad_amounts(client, amount, expected_type):
    err = await invalid_params(client, "trigger_refund",
                               {"customer_id": "CUST-10001", "amount": amount, "reason": GOOD_REASON})
    assert [v["field"] for v in err.data["violations"]] == ["amount"]
    if expected_type:
        assert err.data["violations"][0]["type"] == expected_type


@pytest.mark.parametrize("reason", [
    "too short",                   # 9 characters
    " " * 20,                      # padding doesn't count
    "        a ",                  # strips to one character
    "x" * 501,
    "refund\x00approved by ops",
    "refund​ approved by ops",  # zero-width space
    "refund ‮evah I‬",       # bidi override
    "\x1b[31mapproved by ops\x1b[0m",
])
async def test_bad_reasons(client, reason):
    err = await invalid_params(client, "trigger_refund",
                               {"customer_id": "CUST-10001", "amount": 10, "reason": reason})
    assert err.data["violations"][0]["field"] == "reason"


async def test_reason_at_exactly_the_minimum(client):
    result = await call(client, "trigger_refund",
                        {"customer_id": "CUST-10001", "amount": 10, "reason": "  0123456789  "})
    assert result.is_error is False
    assert result.structured_content["reason"] == "0123456789"


async def test_every_violation_is_reported_at_once(client):
    err = await invalid_params(client, "trigger_refund",
                               {"customer_id": "nope", "amount": -1, "reason": "x"})
    assert sorted(v["field"] for v in err.data["violations"]) == ["amount", "customer_id", "reason"]


async def test_violations_never_echo_the_input(client):
    secret = "4111111111111111"
    err = await invalid_params(client, "get_customer_record", {"customer_id": secret})
    assert secret not in json.dumps(err.model_dump())


@pytest.mark.parametrize("customer_id,code", [
    ("CUST-10003", "account_not_active"),   # frozen
    ("CUST-10004", "account_not_active"),   # closed
    ("CUST-99999", "customer_not_found"),
])
async def test_refund_business_rules(client, customer_id, code):
    result = await call(client, "trigger_refund",
                        {"customer_id": customer_id, "amount": 1, "reason": GOOD_REASON})
    assert result.is_error is True
    assert json.loads(result.content[0].text)["error"] == code


async def test_refund_over_balance(client):
    result = await call(client, "trigger_refund",
                        {"customer_id": "CUST-10002", "amount": 120.01, "reason": GOOD_REASON})
    assert result.is_error is True
    body = json.loads(result.content[0].text)
    assert body["error"] == "insufficient_refundable_balance"
    assert body["refundable_balance_usd"] == 120.0


async def test_idempotency_key_replays_instead_of_paying_twice(client):
    args = {"customer_id": "CUST-10002", "amount": 20, "reason": GOOD_REASON,
            "idempotency_key": "retry-test-0001"}
    first = (await call(client, "trigger_refund", args)).structured_content
    second = (await call(client, "trigger_refund", args)).structured_content
    assert second["refund_id"] == first["refund_id"]
    assert second["status"] == "replayed"
    record = (await call(client, "get_customer_record", {"customer_id": "CUST-10002"})).structured_content
    assert record["refundable_balance_usd"] == 100.0


async def test_idempotency_key_reused_for_a_different_refund(client):
    base = {"customer_id": "CUST-10002", "reason": GOOD_REASON, "idempotency_key": "retry-test-0002"}
    await call(client, "trigger_refund", {**base, "amount": 5})
    result = await call(client, "trigger_refund", {**base, "amount": 6})
    assert result.is_error is True
    assert json.loads(result.content[0].text)["error"] == "idempotency_key_reused"


async def test_concurrent_refunds_cannot_overdraw():
    # CUST-10001 has $1200 refundable; 20 concurrent $100 refunds, exactly 12 can win.
    async with Client(build_server()) as c:
        results = await asyncio.gather(*[
            c.call_tool("trigger_refund", {"customer_id": "CUST-10001", "amount": 100, "reason": GOOD_REASON})
            for _ in range(20)
        ])
    assert sum(not r.is_error for r in results) == 12


# --- protocol ----------------------------------------------------------------

async def test_unknown_tool_is_invalid_params(client):
    err = await invalid_params(client, "admin_reset_key", {"tenant": "acme"})
    assert err.data["available"] == ["get_customer_record", "trigger_refund"]


async def test_crash_is_internal_error_with_no_detail():
    class Exploding(BillingStore):
        def get_customer(self, customer_id):
            raise RuntimeError("connect to postgres://billing:hunter2@10.0.3.7 failed")

    async with Client(build_server(Exploding.seeded())) as c:
        with pytest.raises(MCPError) as info:
            await c.call_tool("get_customer_record", {"customer_id": "CUST-10001"})
    err = info.value.error
    assert err.code == types.INTERNAL_ERROR
    assert err.message == "Internal error"
    assert "hunter2" not in json.dumps(err.model_dump())
    assert set(err.data) == {"call_id"}


async def test_published_schema_matches_what_is_enforced(client):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"get_customer_record", "trigger_refund"}

    lookup = tools["get_customer_record"].input_schema
    assert lookup["properties"]["customer_id"]["pattern"] == "^CUST-[0-9]{5}$"
    assert lookup["additionalProperties"] is False
    assert lookup["required"] == ["customer_id"]

    refund = tools["trigger_refund"].input_schema
    assert refund["properties"]["amount"]["exclusiveMinimum"] == 0
    assert refund["properties"]["reason"]["minLength"] == 10
    assert sorted(refund["required"]) == ["amount", "customer_id", "reason"]
    assert tools["trigger_refund"].annotations.destructive_hint is True
