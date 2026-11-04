"""Input and output schemas for the billing tools.

These models are the single source of truth: the JSON Schema a client sees in
tools/list is generated from them, and the same models validate tools/call.
So the advertised contract and the enforced one can't drift apart.
"""

import math
import unicodedata
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

# [0-9] rather than \d. Python's \d matches any Unicode digit, so
# "CUST-١٢٣٤٥" (Arabic-Indic digits) would sail through a \d pattern and then
# miss every lookup. JSON Schema's \d is ASCII-only, so [0-9] also keeps the
# published schema and the server's check meaning the same thing.
CUSTOMER_ID_PATTERN = r"^CUST-[0-9]{5}$"

CustomerId = Annotated[
    str,
    StringConstraints(pattern=CUSTOMER_ID_PATTERN),
    Field(description="Customer id, CUST- followed by exactly five digits.", examples=["CUST-10001"]),
]

MAX_REFUND_USD = 10_000.0
REASON_MIN, REASON_MAX = 10, 500


class _Strict(BaseModel):
    # strict: "50" is not 50 and "true" is not true. Coercion is how a caller
    # bug turns into a refund nobody meant to send.
    # forbid: a typo'd field ("resaon") is rejected instead of silently dropped.
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


class GetCustomerRecordInput(_Strict):
    customer_id: CustomerId


class TriggerRefundInput(_Strict):
    customer_id: CustomerId
    amount: float = Field(
        gt=0,
        le=MAX_REFUND_USD,
        description="Refund amount in USD. Positive, at most two decimal places.",
        examples=[49.99],
    )
    reason: str = Field(
        min_length=REASON_MIN,
        max_length=REASON_MAX,
        description="Why the refund is being issued. At least 10 characters, kept on the audit trail.",
    )
    idempotency_key: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_-]{8,64}$",
        description="Optional. Retrying with the same key returns the original refund instead of paying twice.",
    )

    @field_validator("amount", mode="before")
    @classmethod
    def _no_bools(cls, v):
        # bool is a subclass of int, so {"amount": true} would otherwise be a
        # $1.00 refund. strict=True already catches this for float fields in
        # current pydantic; the check stays so that's not load-bearing.
        if isinstance(v, bool):
            raise ValueError("amount must be a number, not a boolean")
        return v

    @field_validator("amount")
    @classmethod
    def _whole_cents(cls, v):
        if not math.isfinite(v):
            raise ValueError("amount must be finite")
        # repr() gives the shortest string that round-trips, so 19.99 stays
        # "19.99" here instead of 19.989999999999998436805981327779591083526611328125.
        if Decimal(repr(v)).as_tuple().exponent < -2:
            raise ValueError("amount can't have more than two decimal places")
        return v

    @field_validator("reason", mode="before")
    @classmethod
    def _clean_reason(cls, v):
        if not isinstance(v, str):
            return v  # let strict mode produce the type error
        if any(unicodedata.category(ch) in ("Cc", "Cf") and ch not in "\t\n" for ch in v):
            # Control and format characters (NUL, ANSI escapes, zero-width,
            # bidi overrides) have no business in an audit field and are a
            # classic way to make a log line lie about what it says.
            raise ValueError("reason contains control or invisible characters")
        # Strip before the length check, otherwise ten spaces is a valid reason.
        return v.strip()


class CustomerRecord(BaseModel):
    customer_id: str
    name: str
    email: str
    phone: str
    status: Literal["active", "frozen", "closed"]
    plan: str
    lifetime_spend_usd: float
    refundable_balance_usd: float
    customer_since: str


class RefundReceipt(BaseModel):
    refund_id: str
    customer_id: str
    amount_usd: float
    reason: str
    status: Literal["issued", "replayed"]
    remaining_refundable_usd: float
    issued_at: str
