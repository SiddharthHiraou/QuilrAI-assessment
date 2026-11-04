"""Billing MCP server: get_customer_record and trigger_refund.

Built on the low-level Server rather than MCPServer. MCPServer turns argument
validation failures into isError tool results; the brief wants them rejected
with JSON-RPC error codes, which needs control over what gets raised.
"""

import json
import os
import subprocess
import sys
import uuid

import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from tollgate.log import Log
from tollgate.mcp_server.models import (
    CustomerRecord,
    GetCustomerRecordInput,
    RefundReceipt,
    TriggerRefundInput,
)
from tollgate.mcp_server.store import BillingStore, DomainError

log = Log("mcp_server")

NAME = "tollgate-billing"
VERSION = "0.1.0"


def _chaos(where):
    """TOLLGATE_CHAOS_STDOUT=1 makes every handler try to corrupt stdout.

    Three different ways, because they fail differently: the sys.stdout object,
    the raw file descriptor, and a child process that inherits fd 1. The stdio
    tests run with this on, so they prove the isolation holds instead of
    proving this file happens not to print.
    """
    if os.environ.get("TOLLGATE_CHAOS_STDOUT") != "1":
        return
    print("chaos: print() from " + where)
    os.write(1, b"chaos: os.write(1) from " + where.encode() + b"\n")
    subprocess.run([sys.executable, "-c", "print('chaos: child process')"], check=False)


def _schema(model):
    schema = model.model_json_schema()
    schema.pop("title", None)
    return schema


BILLING_TOOLS = [
    types.Tool(
        name="get_customer_record",
        title="Get customer record",
        description=(
            "Look up a billing customer by id (CUST- plus five digits). Returns contact "
            "details, account status, lifetime spend and the remaining refundable balance."
        ),
        input_schema=_schema(GetCustomerRecordInput),
        output_schema=_schema(CustomerRecord),
        annotations=types.ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False,
        ),
    ),
    types.Tool(
        name="trigger_refund",
        title="Trigger refund",
        description=(
            "Refund a customer against their refundable balance. Moves real money and can't "
            "be undone here. The account must be active. Pass idempotency_key so a retry "
            "can't pay out twice."
        ),
        input_schema=_schema(TriggerRefundInput),
        output_schema=_schema(RefundReceipt),
        annotations=types.ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False,
        ),
    ),
]

# Only exposed when this runs as the downstream behind the gateway, so task 2
# has admin_ tools to gate. The stdio server for task 1 lists exactly two tools.
ADMIN_TOOLS = [
    types.Tool(
        name="admin_reset_key",
        title="Reset tenant API key",
        description="Rotate a tenant's API signing key. Existing keys stop working immediately.",
        input_schema={
            "type": "object",
            "properties": {"tenant": {"type": "string", "pattern": "^[a-z0-9-]{3,32}$"}},
            "required": ["tenant"],
            "additionalProperties": False,
        },
        annotations=types.ToolAnnotations(destructive_hint=True, idempotent_hint=False),
    ),
    types.Tool(
        name="admin_list_refunds",
        title="List all refunds",
        description="Every refund issued so far, across all customers.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        annotations=types.ToolAnnotations(read_only_hint=True),
    ),
]


def violations(exc):
    # Field and rule, never the value. Echoing input back is how a card number
    # someone pasted into the wrong field ends up in a log aggregator.
    return [
        {"field": ".".join(str(p) for p in e["loc"]) or "(root)", "issue": e["msg"], "type": e["type"]}
        for e in exc.errors(include_url=False, include_input=False, include_context=False)
    ]


def _ok(model):
    data = model.model_dump(mode="json")
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(data))],
        structured_content=data,
        is_error=False,
    )


def _tool_error(exc):
    body = {"error": exc.code, "message": exc.message, **exc.details}
    # No structured_content: outputSchema describes the success shape, and a
    # client that validates against it shouldn't choke on an error result.
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(body))],
        is_error=True,
    )


def build_server(store=None, admin_tools=False):
    billing = store or BillingStore.seeded()
    tools = BILLING_TOOLS + (ADMIN_TOOLS if admin_tools else [])
    by_name = {t.name: t for t in tools}

    async def list_tools(ctx, params):
        _chaos("tools/list")
        return types.ListToolsResult(tools=tools)

    async def call_tool(ctx, params):
        call_id = uuid.uuid4().hex[:10]
        name = params.name
        _chaos("tools/call")

        if name not in by_name:
            # -32602 and not -32601: tools/call exists, the name param is what's wrong.
            log.warning("unknown tool", tool=name, call_id=call_id)
            raise MCPError(types.INVALID_PARAMS, f"Unknown tool: {name}",
                           {"tool": name, "available": sorted(by_name)})

        try:
            if name == "get_customer_record":
                args = GetCustomerRecordInput.model_validate(params.arguments or {})
                customer = billing.get_customer(args.customer_id)
                return _ok(CustomerRecord(**vars(customer)))

            if name == "trigger_refund":
                args = TriggerRefundInput.model_validate(params.arguments or {})
                refund, replayed = await billing.refund(
                    args.customer_id, args.amount, args.reason, args.idempotency_key)
                remaining = billing.get_customer(args.customer_id).refundable_balance_usd
                log.info("refund", call_id=call_id, refund_id=refund.refund_id,
                         customer_id=refund.customer_id, amount_usd=refund.amount_usd,
                         replayed=replayed)
                return _ok(RefundReceipt(
                    **vars(refund),
                    status="replayed" if replayed else "issued",
                    remaining_refundable_usd=remaining,
                ))

            return _admin(name, params.arguments or {}, billing)

        except ValidationError as exc:
            detail = violations(exc)
            log.info("rejected arguments", tool=name, call_id=call_id, violations=detail)
            raise MCPError(types.INVALID_PARAMS, f"Invalid arguments for {name}",
                           {"tool": name, "violations": detail}) from None
        except DomainError as exc:
            log.info("tool error", tool=name, call_id=call_id, code=exc.code)
            return _tool_error(exc)
        except MCPError:
            raise
        except Exception:
            # Full detail to stderr under call_id; the wire gets the id and nothing else.
            log.exception("tool crashed", tool=name, call_id=call_id)
            raise MCPError(types.INTERNAL_ERROR, "Internal error", {"call_id": call_id}) from None

    return Server(
        NAME,
        version=VERSION,
        title="Tollgate billing",
        instructions=(
            "Call get_customer_record before trigger_refund to check the account is active "
            "and the refundable balance covers the amount."
        ),
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def _admin(name, arguments, billing):
    if name == "admin_reset_key":
        tenant = arguments.get("tenant")
        if not isinstance(tenant, str) or set(arguments) != {"tenant"}:
            raise MCPError(types.INVALID_PARAMS, "Invalid arguments for admin_reset_key",
                           {"tool": name, "violations": [{"field": "tenant", "issue": "required string"}]})
        body = {"tenant": tenant, "rotated": True, "new_key_fingerprint": uuid.uuid4().hex[:12]}
    else:
        body = {"refunds": [vars(r) for r in billing.refunds()]}
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(body))])
