"""Request models — second validation layer after the Gateway's JSON-schema check.

``strict=True`` disables type coercion, so an LLM sending ``"amount_cents": "one thousand"``
or ``"amount_cents": "5000"`` is rejected with INVALID_PARAMETERS instead of being guessed at.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

CUSTOMER_ID_PATTERN = r"^CUST-\d{4,10}$"
ORDER_ID_PATTERN = r"^[A-Za-z0-9-]{1,32}$"
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9_.:-]{8,128}$"

# Sanity ceiling for input validation only; the business limit lives in Settings / Cedar.
_MAX_REFUND_INPUT_CENTS = 100_000_000


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True, frozen=True)


class GetOrderStatusRequest(_Request):
    customer_id: str = Field(pattern=CUSTOMER_ID_PATTERN)
    order_id: str = Field(pattern=ORDER_ID_PATTERN)


class GetCustomerRequest(_Request):
    customer_id: str = Field(pattern=CUSTOMER_ID_PATTERN)


class RefundRequest(_Request):
    customer_id: str = Field(pattern=CUSTOMER_ID_PATTERN)
    order_id: str = Field(pattern=ORDER_ID_PATTERN)
    amount_cents: int = Field(gt=0, le=_MAX_REFUND_INPUT_CENTS)
    reason: str = Field(min_length=3, max_length=500)
    idempotency_key: str = Field(pattern=IDEMPOTENCY_KEY_PATTERN)
