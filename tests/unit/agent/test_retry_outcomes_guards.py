from __future__ import annotations

import json

import pytest
from support_agent.guards import ToolCallBudget
from support_agent.identity import jwt_expiry
from support_agent.outcomes import Outcome, classify
from support_agent.retry import BackoffPolicy


def _result(body: object, status: str = "success") -> dict:
    text = body if isinstance(body, str) else json.dumps(body)
    return {"toolUseId": "t1", "status": status, "content": [{"text": text}]}


class TestBackoff:
    def test_delay_grows_exponentially_and_is_capped(self) -> None:
        policy = BackoffPolicy(max_attempts=5, base_delay_seconds=0.5, max_delay_seconds=2.0)
        upper = [policy.delay_for(n, rng=lambda: 1.0) for n in (1, 2, 3, 4)]
        lower = [policy.delay_for(n, rng=lambda: 0.0) for n in (1, 2, 3, 4)]
        assert upper == [0.5, 1.0, 2.0, 2.0]
        assert lower == [0.25, 0.5, 1.0, 1.0]

    def test_should_retry_respects_max_attempts(self) -> None:
        policy = BackoffPolicy(max_attempts=3)
        assert [policy.should_retry(n) for n in (1, 2, 3)] == [True, True, False]


class TestClassification:
    def test_success_contract(self) -> None:
        assert classify(_result({"ok": True, "data": {}})).outcome is Outcome.SUCCESS

    def test_retryable_backend_error(self) -> None:
        classified = classify(
            _result(
                {
                    "ok": False,
                    "error": {
                        "code": "DOWNSTREAM_TIMEOUT",
                        "http_status": 504,
                        "retryable": True,
                        "message": "t",
                    },
                }
            )
        )
        assert classified.outcome is Outcome.RETRYABLE_ERROR
        assert classified.http_status == 504

    def test_business_rejection_is_not_retried(self) -> None:
        classified = classify(
            _result({"ok": False, "error": {"code": "INVALID_PARAMETERS", "retryable": False}})
        )
        assert classified.outcome is Outcome.FATAL_ERROR

    @pytest.mark.parametrize(
        "text",
        [
            "Tool execution failed: Request denied by policy engine",
            "AccessDeniedException: not authorized to call support-tools___refund_customer",
        ],
    )
    def test_policy_denial_detected(self, text: str) -> None:
        assert classify(_result(text, status="error")).outcome is Outcome.POLICY_DENIED

    def test_transient_gateway_error_is_retryable(self) -> None:
        assert classify(_result("Tool execution failed: 503 Service Unavailable", "error")).outcome is (
            Outcome.RETRYABLE_ERROR
        )

    def test_unknown_gateway_error_is_fatal(self) -> None:
        assert (
            classify(_result("Tool execution failed: invalid arguments", "error")).outcome
            is Outcome.FATAL_ERROR
        )


class TestToolCallBudget:
    def test_identical_calls_are_limited(self) -> None:
        budget = ToolCallBudget(max_calls=10, max_identical_calls=2)
        args = {"order_id": "999"}
        assert budget.check("get_order_status", args) is None
        assert budget.check("get_order_status", args) is None
        violation = budget.check("get_order_status", args)
        assert violation is not None and violation.code == "REPEATED_TOOL_CALL"

    def test_different_arguments_are_distinct(self) -> None:
        budget = ToolCallBudget(max_calls=10, max_identical_calls=1)
        assert budget.check("get_order_status", {"order_id": "1"}) is None
        assert budget.check("get_order_status", {"order_id": "2"}) is None

    def test_total_budget(self) -> None:
        budget = ToolCallBudget(max_calls=2, max_identical_calls=5)
        budget.check("a", {})
        budget.check("b", {})
        violation = budget.check("c", {})
        assert violation is not None and violation.code == "TOOL_BUDGET_EXCEEDED"


def test_jwt_expiry_reads_exp_claim() -> None:
    import base64

    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1_900_000_000}).encode()).decode().rstrip("=")
    assert jwt_expiry(f"header.{payload}.signature") == 1_900_000_000
    assert jwt_expiry("not-a-jwt") is None
