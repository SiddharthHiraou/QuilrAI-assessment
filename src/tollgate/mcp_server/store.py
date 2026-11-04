"""In-memory billing data. Stands in for whatever the real system of record is."""

import asyncio
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime


class DomainError(Exception):
    """The request was well-formed but the business said no.

    These go back as tool results with isError set, not as protocol errors.
    "That account is frozen" is something the model should read and act on.
    """

    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


@dataclass(frozen=True)
class Customer:
    customer_id: str
    name: str
    email: str
    phone: str
    status: str
    plan: str
    lifetime_spend_usd: float
    refundable_balance_usd: float
    customer_since: str


@dataclass(frozen=True)
class Refund:
    refund_id: str
    customer_id: str
    amount_usd: float
    reason: str
    issued_at: str


SEED = [
    Customer("CUST-10001", "Priya Raman", "priya.raman@example.com", "(415) 555-0132",
             "active", "business", 18240.50, 1200.00, "2021-03-14"),
    Customer("CUST-10002", "Marcus Chen", "marcus.chen@example.org", "(206) 555-0187",
             "active", "starter", 940.00, 120.00, "2023-08-02"),
    Customer("CUST-10003", "Elena Sokolova", "elena.s@example.net", "(312) 555-0144",
             "frozen", "business", 7310.25, 500.00, "2022-01-20"),
    Customer("CUST-10004", "Daniel Okafor", "d.okafor@example.com", "(646) 555-0199",
             "closed", "starter", 85.00, 0.00, "2024-05-11"),
]


def _cents(x):
    return round(x * 100)


class BillingStore:
    def __init__(self, customers):
        self._customers = {c.customer_id: c for c in customers}
        self._refunds = {}
        self._by_idempotency_key = {}
        # Check-then-debit has to be atomic or two concurrent refunds can both
        # pass the balance check. Single event loop, but the await points in a
        # real implementation (DB calls) would open that window.
        self._lock = asyncio.Lock()

    @classmethod
    def seeded(cls):
        return cls(SEED)

    def refunds(self):
        return list(self._refunds.values())

    def get_customer(self, customer_id):
        customer = self._customers.get(customer_id)
        if customer is None:
            raise DomainError("customer_not_found", f"No customer with id {customer_id}.",
                              customer_id=customer_id)
        return customer

    async def refund(self, customer_id, amount, reason, idempotency_key=None):
        async with self._lock:
            if idempotency_key is not None and idempotency_key in self._by_idempotency_key:
                prior = self._refunds[self._by_idempotency_key[idempotency_key]]
                if (prior.customer_id, _cents(prior.amount_usd)) != (customer_id, _cents(amount)):
                    raise DomainError(
                        "idempotency_key_reused",
                        "That idempotency key was already used for a different refund.",
                    )
                return prior, True

            customer = self.get_customer(customer_id)
            if customer.status != "active":
                raise DomainError(
                    "account_not_active",
                    f"Refunds need an active account; {customer_id} is {customer.status}.",
                    status=customer.status,
                )
            # Integer cents for the comparison so 0.1 + 0.2 doesn't decide a refund.
            if _cents(amount) > _cents(customer.refundable_balance_usd):
                raise DomainError(
                    "insufficient_refundable_balance",
                    f"Refund of ${amount:.2f} exceeds the refundable balance "
                    f"of ${customer.refundable_balance_usd:.2f}.",
                    refundable_balance_usd=customer.refundable_balance_usd,
                )

            refund = Refund(
                refund_id="RF-" + secrets.token_hex(4).upper(),
                customer_id=customer_id,
                amount_usd=amount,
                reason=reason,
                issued_at=datetime.now(UTC).isoformat(timespec="seconds"),
            )
            remaining = (_cents(customer.refundable_balance_usd) - _cents(amount)) / 100
            self._customers[customer_id] = replace(customer, refundable_balance_usd=remaining)
            self._refunds[refund.refund_id] = refund
            if idempotency_key is not None:
                self._by_idempotency_key[idempotency_key] = refund.refund_id
            return refund, False
