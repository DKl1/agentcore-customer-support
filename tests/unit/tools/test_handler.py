"""Lambda contract: routing, validation and the never-raise error envelope."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from support_tools import handler
from support_tools.models import GetCustomerRequest, GetOrderStatusRequest, RefundRequest

from .conftest import TABLES, lambda_context

SCHEMAS = json.loads(
    (Path(__file__).resolve().parents[3] / "src" / "tools" / "tool_schemas.json").read_text()
)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, ddb: Any) -> None:
    monkeypatch.setenv("CUSTOMERS_TABLE", TABLES.customers)
    monkeypatch.setenv("ORDERS_TABLE", TABLES.orders)
    monkeypatch.setenv("REFUNDS_TABLE", TABLES.refunds)
    monkeypatch.setenv("IDEMPOTENCY_TABLE", TABLES.idempotency)
    monkeypatch.setenv("FAULT_INJECTION_ENABLED", "true")
    monkeypatch.setenv("DOWNSTREAM_TIMEOUT_SECONDS", "0.01")
    handler._container.cache_clear()


def test_get_order_status_success() -> None:
    result = handler.lambda_handler(
        {"customer_id": "CUST-1001", "order_id": "123"}, lambda_context("get_order_status")
    )
    assert result["ok"] is True
    assert result["data"]["status"] == "DELAYED"


@pytest.mark.parametrize(
    "arguments",
    [
        {
            "customer_id": "CUST-1001",
            "order_id": "123",
            "amount_cents": "one thousand dollars",
            "reason": "late",
            "idempotency_key": "operation-123",
        },
        {
            "customer_id": "CUST-1001",
            "order_id": "123",
            "amount_cents": "5000",
            "reason": "late",
            "idempotency_key": "operation-123",
        },
        {
            "customer_id": "CUST-1001",
            "order_id": "123",
            "amount_cents": -5,
            "reason": "late",
            "idempotency_key": "operation-123",
        },
        {
            "customer_id": "CUST-1001",
            "order_id": "123",
            "amount_cents": 100,
            "reason": "late",
            "idempotency_key": "operation-123",
            "unexpected": True,
        },
    ],
)
def test_invalid_parameters_are_rejected_without_side_effects(arguments: dict[str, Any], ddb: Any) -> None:
    result = handler.lambda_handler(arguments, lambda_context("refund_customer"))

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_PARAMETERS"
    assert result["error"]["retryable"] is False
    assert ddb.scan(TableName=TABLES.refunds)["Items"] == []
    assert "one thousand" not in json.dumps(result), "raw input values must not be echoed"


def test_unknown_tool_is_rejected() -> None:
    result = handler.lambda_handler({}, lambda_context("delete_customer"))
    assert result["error"]["code"] == "UNKNOWN_TOOL"


def test_http_500_from_payment_provider_is_structured_and_retryable() -> None:
    result = handler.lambda_handler(
        {
            "customer_id": "CUST-1001",
            "order_id": "500",
            "amount_cents": 1000,
            "reason": "broken",
            "idempotency_key": "operation-500",
        },
        lambda_context("refund_customer"),
    )
    assert result["error"] == {
        "code": "DOWNSTREAM_UNAVAILABLE",
        "message": "Payment provider returned HTTP 500 Internal Server Error",
        "http_status": 500,
        "retryable": True,
        "details": {"dependency": "payment-provider"},
    }


def test_unexpected_exception_becomes_internal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: Any) -> Any:
        raise RuntimeError("bug")

    monkeypatch.setattr(handler, "dispatch", boom)
    result = handler.lambda_handler({"customer_id": "CUST-1001"}, lambda_context("get_customer"))
    assert result["error"]["code"] == "INTERNAL_ERROR"
    assert "bug" not in result["error"]["message"]


@pytest.mark.parametrize(
    ("tool", "model"),
    [
        ("get_order_status", GetOrderStatusRequest),
        ("get_customer", GetCustomerRequest),
        ("refund_customer", RefundRequest),
    ],
)
def test_gateway_schema_matches_backend_model(tool: str, model: Any) -> None:
    """Contract test: the schema published to the Gateway and the Lambda model must agree."""
    schema = next(s for s in SCHEMAS if s["name"] == tool)["inputSchema"]
    model_schema = model.model_json_schema()
    assert set(schema["properties"]) == set(model_schema["properties"])
    assert set(schema["required"]) == set(model_schema["required"])
