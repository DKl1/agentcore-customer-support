"""Gateway tool proxy: argument binding, retries with backoff, loop guard, policy denial handling."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

pytest.importorskip("strands")

from support_agent.contracts import InvocationContext
from support_agent.guards import ToolCallBudget, ToolCallLedger
from support_agent.retry import BackoffPolicy
from support_agent.tool_proxy import GatewayToolProxy

CONTEXT = InvocationContext(
    customer_id="CUST-1001", session_id="CUST-1001-" + "a" * 32, operation_id="operation-123"
)

REFUND_SPEC = {
    "name": "support-tools___refund_customer",
    "description": "refund",
    "inputSchema": {
        "json": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "order_id": {"type": "string"},
                "amount_cents": {"type": "integer"},
                "reason": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["customer_id", "order_id", "amount_cents", "reason", "idempotency_key"],
        }
    },
}


def ok(data: dict | None = None) -> dict:
    return {"status": "success", "content": [{"text": json.dumps({"ok": True, "data": data or {}})}]}


def backend_error(code: str, retryable: bool, http_status: int = 500) -> dict:
    body = {
        "ok": False,
        "error": {"code": code, "retryable": retryable, "http_status": http_status, "message": code},
    }
    return {"status": "success", "content": [{"text": json.dumps(body)}]}


class FakeGateway:
    def __init__(self, *results: dict) -> None:
        self._results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def call_tool_async(
        self,
        tool_use_id: str,
        name: str,
        arguments: dict | None = None,
        read_timeout_seconds: timedelta | None = None,
    ) -> dict:
        self.calls.append({"name": name, "arguments": arguments})
        return self._results.pop(0) if len(self._results) > 1 else self._results[0]


def make_proxy(
    gateway: FakeGateway, *, budget: ToolCallBudget | None = None
) -> tuple[GatewayToolProxy, list, ToolCallLedger]:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    ledger = ToolCallLedger()
    proxy = GatewayToolProxy(
        mcp_tool_name="support-tools___refund_customer",
        public_name="refund_customer",
        tool_spec=REFUND_SPEC,  # type: ignore[arg-type]
        client=gateway,
        context=CONTEXT,
        budget=budget or ToolCallBudget(max_calls=8, max_identical_calls=2),
        ledger=ledger,
        backoff=BackoffPolicy(max_attempts=3, base_delay_seconds=0.5, max_delay_seconds=4.0),
        call_timeout_seconds=5,
        sleep=fake_sleep,
    )
    return proxy, sleeps, ledger


def tool_use(**arguments: Any) -> dict:
    return {"toolUseId": "tool-1", "name": "refund_customer", "input": arguments}


def test_model_never_sees_platform_bound_arguments() -> None:
    proxy, _, _ = make_proxy(FakeGateway(ok()))
    schema = proxy.tool_spec["inputSchema"]["json"]
    assert "customer_id" not in schema["properties"]
    assert "idempotency_key" not in schema["properties"]
    assert set(schema["required"]) == {"order_id", "amount_cents", "reason"}
    assert proxy.tool_name == "refund_customer"


async def test_customer_and_idempotency_key_are_bound_by_platform() -> None:
    gateway = FakeGateway(ok())
    proxy, _, _ = make_proxy(gateway)

    await proxy.execute(
        tool_use(order_id="123", amount_cents=10_000, reason="late", idempotency_key="llm-made-up")
    )

    sent = gateway.calls[0]["arguments"]
    assert sent["customer_id"] == "CUST-1001"
    assert sent["idempotency_key"] == "operation-123"


async def test_other_customer_is_rejected_without_calling_gateway() -> None:
    gateway = FakeGateway(ok())
    proxy, _, ledger = make_proxy(gateway)

    result = await proxy.execute(
        tool_use(customer_id="CUST-2002", order_id="321", amount_cents=100, reason="x")
    )

    assert result["status"] == "error"
    assert "CUSTOMER_SCOPE_VIOLATION" in result["content"][0]["text"]
    assert gateway.calls == []
    assert ledger.records[0].error_code == "CUSTOMER_SCOPE_VIOLATION"


async def test_retryable_error_is_retried_with_exponential_backoff_then_succeeds() -> None:
    gateway = FakeGateway(
        backend_error("DOWNSTREAM_UNAVAILABLE", True), backend_error("DOWNSTREAM_UNAVAILABLE", True), ok()
    )
    proxy, sleeps, ledger = make_proxy(gateway)

    result = await proxy.execute(tool_use(order_id="123", amount_cents=100, reason="late"))

    assert result["status"] == "success"
    assert len(gateway.calls) == 3
    assert len(sleeps) == 2 and 0.25 <= sleeps[0] <= 0.5 and 0.5 <= sleeps[1] <= 1.0
    assert {call["arguments"]["idempotency_key"] for call in gateway.calls} == {"operation-123"}, (
        "every retry must carry the same idempotency key"
    )
    assert ledger.records[0].attempts == 3


async def test_retries_exhausted_adds_platform_note() -> None:
    gateway = FakeGateway(backend_error("DOWNSTREAM_TIMEOUT", True, 504))
    proxy, sleeps, ledger = make_proxy(gateway)

    result = await proxy.execute(tool_use(order_id="777", amount_cents=100, reason="late"))

    assert len(gateway.calls) == 3 and len(sleeps) == 2
    assert result["status"] == "error"
    assert "Do not call this tool again" in result["content"][-1]["text"]
    assert ledger.records[0].outcome == "retryable_error"


async def test_non_retryable_error_is_not_retried() -> None:
    gateway = FakeGateway(backend_error("INVALID_PARAMETERS", False, 400))
    proxy, sleeps, _ = make_proxy(gateway)

    result = await proxy.execute(tool_use(order_id="123", amount_cents=100, reason="late"))

    assert len(gateway.calls) == 1 and sleeps == []
    assert result["status"] == "error"


async def test_policy_denial_is_not_retried() -> None:
    denied = {"status": "error", "content": [{"text": "Tool execution failed: denied by policy"}]}
    gateway = FakeGateway(denied)
    proxy, sleeps, ledger = make_proxy(gateway)

    result = await proxy.execute(tool_use(order_id="123", amount_cents=500_000, reason="injection"))

    assert len(gateway.calls) == 1 and sleeps == []
    assert "DENIED by the authorization policy" in result["content"][-1]["text"]
    assert ledger.records[0].outcome == "policy_denied"


async def test_transport_exception_is_treated_as_transient() -> None:
    class Flaky(FakeGateway):
        async def call_tool_async(self, *args: Any, **kwargs: Any) -> dict:
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise ConnectionError("reset by peer")
            return ok()

    gateway = Flaky(ok())
    proxy, _, _ = make_proxy(gateway)
    result = await proxy.execute(tool_use(order_id="123", amount_cents=100, reason="late"))
    assert result["status"] == "success"
    assert len(gateway.calls) == 2


async def test_loop_guard_blocks_repeated_identical_calls() -> None:
    gateway = FakeGateway(ok())
    proxy, _, ledger = make_proxy(gateway, budget=ToolCallBudget(max_calls=8, max_identical_calls=2))

    for _ in range(2):
        await proxy.execute(tool_use(order_id="999", amount_cents=100, reason="x"))
    blocked = await proxy.execute(tool_use(order_id="999", amount_cents=100, reason="x"))

    assert len(gateway.calls) == 2
    assert "REPEATED_TOOL_CALL" in blocked["content"][0]["text"]
    assert ledger.records[-1].outcome == "rejected_by_guard"
