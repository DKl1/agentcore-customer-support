"""Refund business rules and idempotency (scenario: 'retry does not create a duplicate refund')."""

from __future__ import annotations

from typing import Any

import pytest
from support_tools.errors import (
    DownstreamUnavailable,
    IdempotencyKeyReused,
    NotFound,
    OperationInProgress,
    RefundExceedsBalance,
    RefundLimitExceeded,
)
from support_tools.idempotency import IdempotencyStore
from support_tools.models import RefundRequest
from support_tools.repository import from_item, to_item
from support_tools.services import RefundService

from .conftest import TABLES


def _refund(
    amount_cents: int = 10_000, key: str = "operation-123", order_id: str = "123", **kw: Any
) -> RefundRequest:
    return RefundRequest(
        customer_id=kw.get("customer_id", "CUST-1001"),
        order_id=order_id,
        amount_cents=amount_cents,
        reason=kw.get("reason", "Package delayed"),
        idempotency_key=key,
    )


def _refund_rows(ddb: Any) -> list[dict[str, Any]]:
    return [from_item(item) for item in ddb.scan(TableName=TABLES.refunds)["Items"]]


def _refunded_cents(ddb: Any, order_id: str) -> int:
    item = ddb.get_item(TableName=TABLES.orders, Key=to_item({"order_id": order_id}))["Item"]
    return int(item["refunded_cents"]["N"])


def test_refund_within_limit_succeeds(refunds: RefundService, ddb: Any) -> None:
    result = refunds.refund(_refund(10_000))

    assert result["status"] == "SUCCEEDED"
    assert result["idempotent_replay"] is False
    assert _refunded_cents(ddb, "123") == 10_000
    assert len(_refund_rows(ddb)) == 1


def test_refund_exactly_at_limit_is_allowed(refunds: RefundService) -> None:
    assert refunds.refund(_refund(100_000))["status"] == "SUCCEEDED"


def test_retry_with_same_idempotency_key_returns_existing_refund(refunds: RefundService, ddb: Any) -> None:
    first = refunds.refund(_refund(10_000, key="operation-123"))
    retry = refunds.refund(_refund(10_000, key="operation-123", reason="paraphrased reason on retry"))

    assert retry["refund_id"] == first["refund_id"]
    assert retry["idempotent_replay"] is True
    assert len(_refund_rows(ddb)) == 1, "a retry must never create a second refund"
    assert _refunded_cents(ddb, "123") == 10_000, "money must move only once"


def test_same_key_with_different_amount_is_rejected(refunds: RefundService, ddb: Any) -> None:
    refunds.refund(_refund(10_000, key="operation-123"))

    with pytest.raises(IdempotencyKeyReused):
        refunds.refund(_refund(20_000, key="operation-123"))
    assert len(_refund_rows(ddb)) == 1


def test_idempotency_keys_are_scoped_per_customer(refunds: RefundService, ddb: Any) -> None:
    refunds.refund(_refund(10_000, key="operation-123"))
    other = refunds.refund(_refund(5_000, key="operation-123", customer_id="CUST-2002", order_id="321"))

    assert other["idempotent_replay"] is False
    assert len(_refund_rows(ddb)) == 2


def test_concurrent_attempt_in_progress_is_retryable(
    refunds: RefundService, idempotency: IdempotencyStore
) -> None:
    from support_tools.idempotency import request_fingerprint

    pk = IdempotencyStore.scoped_key("CUST-1001", "operation-123")
    idempotency.claim(
        pk, request_fingerprint({"customer_id": "CUST-1001", "order_id": "123", "amount_cents": 10_000})
    )

    with pytest.raises(OperationInProgress) as error:
        refunds.refund(_refund(10_000))
    assert error.value.retryable is True


def test_expired_lock_can_be_reclaimed(ddb: Any) -> None:
    now = [1_000.0]
    store = IdempotencyStore(ddb, TABLES.idempotency, lock_seconds=60, ttl_days=7, clock=lambda: now[0])
    store.claim("CUST-1001#op-expired-1", "fp")

    now[0] += 61  # the first worker crashed and its lock expired
    assert store.claim("CUST-1001#op-expired-1", "fp").is_replay is False


def test_refund_over_limit_is_rejected_by_backend(refunds: RefundService, ddb: Any) -> None:
    with pytest.raises(RefundLimitExceeded):
        refunds.refund(_refund(500_000))
    assert _refund_rows(ddb) == []


def test_refund_cannot_exceed_order_balance(refunds: RefundService) -> None:
    with pytest.raises(RefundExceedsBalance):
        refunds.refund(_refund(4_501, order_id="999", key="operation-999"))


def test_refund_for_other_customers_order_is_not_found(refunds: RefundService) -> None:
    with pytest.raises(NotFound):
        refunds.refund(_refund(1_000, order_id="321"))


def test_payment_provider_500_rolls_back_and_allows_retry(refunds: RefundService, ddb: Any) -> None:
    with pytest.raises(DownstreamUnavailable) as error:
        refunds.refund(_refund(1_000, order_id="500", key="operation-500"))

    assert error.value.http_status == 500
    assert error.value.retryable is True
    assert _refunded_cents(ddb, "500") == 0, "reservation must be compensated"
    assert _refund_rows(ddb) == []
    # lock released -> a retry is attempted again (and fails again, the fault is permanent)
    with pytest.raises(DownstreamUnavailable):
        refunds.refund(_refund(1_000, order_id="500", key="operation-500"))
