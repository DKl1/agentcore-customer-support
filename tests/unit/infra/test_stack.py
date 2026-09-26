"""CDK template assertions for the security-relevant configuration (least privilege, auth modes)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

cdk = pytest.importorskip("aws_cdk")
from aws_cdk.assertions import Match, Template  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "infra"))
from support_agent_infra.config import StageConfig  # noqa: E402
from support_agent_infra.stack import CustomerSupportAgentStack  # noqa: E402

ENGINE_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:123456789012:policy-engine/support_agent_dev_policies-abcdefghij"
)


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App(
        context={
            "stage": "dev",
            "modelId": "us.anthropic.claude-sonnet-4-6",
            "maxRefundCents": 100000,
            "enableFaultInjection": True,
            "enableGuardrail": True,
            "policyEngineArn": ENGINE_ARN,
            "aws:cdk:bundling-stacks": [],  # no Docker in unit tests
        }
    )
    stack = CustomerSupportAgentStack(
        app,
        "CustomerSupportAgent-dev",
        config=StageConfig.from_context(app),
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )
    return Template.from_stack(stack)


def _actions(template: Template, role_logical_prefix: str) -> set[str]:
    actions: set[str] = set()
    for logical_id, policy in template.find_resources("AWS::IAM::Policy").items():
        if not logical_id.startswith(role_logical_prefix):
            continue
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            value = statement["Action"]
            actions.update([value] if isinstance(value, str) else value)
    return actions


def test_gateway_requires_jwt_from_single_client_and_enforces_policy(template: Template) -> None:
    template.has_resource_properties(
        "AWS::BedrockAgentCore::Gateway",
        {
            "AuthorizerType": "CUSTOM_JWT",
            "ProtocolType": "MCP",
            "AuthorizerConfiguration": {
                "CustomJWTAuthorizer": {
                    "AllowedClients": Match.any_value(),
                    "DiscoveryUrl": Match.any_value(),
                }
            },
            "PolicyEngineConfiguration": {"Arn": ENGINE_ARN, "Mode": "ENFORCE"},
        },
    )


def test_gateway_target_uses_iam_role_and_publishes_three_tools(template: Template) -> None:
    targets = template.find_resources("AWS::BedrockAgentCore::GatewayTarget")
    (target,) = targets.values()
    props: dict[str, Any] = target["Properties"]
    assert props["CredentialProviderConfigurations"] == [{"CredentialProviderType": "GATEWAY_IAM_ROLE"}]
    tools = props["TargetConfiguration"]["Mcp"]["Lambda"]["ToolSchema"]["InlinePayload"]
    assert {tool["Name"] for tool in tools} == {"get_order_status", "get_customer", "refund_customer"}


def test_tools_lambda_has_no_scan_or_wildcard_dynamodb_access(template: Template) -> None:
    actions = _actions(template, "ToolsRole")
    assert {"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"} <= actions
    assert "dynamodb:Scan" not in actions and "dynamodb:*" not in actions
    assert "dynamodb:BatchWriteItem" not in actions


def test_runtime_role_is_scoped(template: Template) -> None:
    actions = _actions(template, "RuntimeExecutionRole")
    assert "bedrock:InvokeModel" in actions
    assert "bedrock-agentcore:GetResourceOauth2Token" in actions
    assert not any(action.endswith(":*") for action in actions), "no service-wide wildcards"
    rendered = json.dumps(template.to_json())
    assert "AdministratorAccess" not in rendered


def test_runtime_has_no_secrets_in_environment(template: Template) -> None:
    (runtime,) = template.find_resources("AWS::BedrockAgentCore::Runtime").values()
    env = runtime["Properties"]["EnvironmentVariables"]
    assert not any("SECRET" in key or "PASSWORD" in key for key in env)
    assert env["GATEWAY_CREDENTIAL_PROVIDER"] == "support-agent-dev-gateway-m2m"
    assert runtime["Properties"]["LifecycleConfiguration"]["IdleRuntimeSessionTimeout"] == 900


def test_prod_rejects_fault_injection() -> None:
    app = cdk.App(context={"stage": "prod", "modelId": "m", "enableFaultInjection": True})
    with pytest.raises(ValueError, match="Fault injection"):
        StageConfig.from_context(app)
