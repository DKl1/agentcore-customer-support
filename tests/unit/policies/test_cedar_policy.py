"""Offline evaluation of policies/support_tools.cedar with the Cedar engine (cedarpy).

This checks the *policy logic* (limit boundaries, default deny, forbid precedence) in CI,
before the policies are uploaded to AgentCore Policy, where the same statements are enforced
at the Gateway.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

cedarpy = pytest.importorskip("cedarpy")

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from configure_policy import render_policies  # noqa: E402

GATEWAY_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/support-agent-dev-gateway-abc"
OUTPUTS = {"GatewayArn": GATEWAY_ARN, "GatewayTargetName": "support-tools", "MaxRefundCents": "100000"}
POLICIES = "\n".join(render_policies(OUTPUTS).values())


def decide(tool: str, tool_input: dict) -> bool:
    request = {
        "principal": 'AgentCore::OAuthUser::"agent-client-id"',
        "action": f'AgentCore::Action::"support-tools___{tool}"',
        "resource": f'AgentCore::Gateway::"{GATEWAY_ARN}"',
        "context": {"input": tool_input},
    }
    result = cedarpy.is_authorized(request, POLICIES, [])
    return result.decision == cedarpy.Decision.Allow


REFUND = {"customer_id": "CUST-1001", "order_id": "123", "reason": "late", "idempotency_key": "operation-123"}


@pytest.mark.parametrize(
    ("amount_cents", "allowed"),
    [
        (1, True),
        (10_000, True),  # $100
        (100_000, True),  # $1,000.00 exactly -> ALLOW
        (100_001, False),  # $1,000.01 -> DENY
        (500_000, False),  # $5,000 (prompt injection scenario) -> DENY
        (0, False),
        (-100, False),
    ],
)
def test_refund_limit(amount_cents: int, allowed: bool) -> None:
    assert decide("refund_customer", {**REFUND, "amount_cents": amount_cents}) is allowed


def test_refund_without_amount_is_denied() -> None:
    assert decide("refund_customer", REFUND) is False


@pytest.mark.parametrize("tool", ["get_order_status", "get_customer"])
def test_read_only_tools_allowed(tool: str) -> None:
    assert decide(tool, {"customer_id": "CUST-1001", "order_id": "123"}) is True


def test_unknown_tools_are_denied_by_default() -> None:
    assert decide("delete_customer", {"customer_id": "CUST-1001"}) is False


def test_all_statements_rendered() -> None:
    names = set(render_policies(OUTPUTS))
    assert names == {"allow_read_only_tools", "allow_refund_up_to_limit", "forbid_refund_over_limit"}
