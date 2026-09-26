"""AgentCore Gateway (MCP) with the Lambda tool target.

Inbound : CUSTOM_JWT — only tokens from our Cognito pool, only our agent's client id.
Outbound: GATEWAY_IAM_ROLE — the Gateway role may invoke exactly one Lambda function.
Policy  : a Cedar policy engine is attached in ENFORCE mode (see scripts/configure_policy.py);
          when ``policyEngineArn`` is passed as context, CDK owns the attachment so that
          later deployments cannot silently detach it.
"""

from __future__ import annotations

import json
from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

from ..config import GATEWAY_TARGET_NAME, TOOL_SCHEMAS_FILE, StageConfig
from .identity import GatewayInboundAuth

# Handshake-based MCP revisions the Gateway accepts. 2025-11-25 is the newest handshake revision and the one
# mcp>=2 clients offer first. The stateless 2026-07-28 revision is not added until Gateway documents support for it.
MCP_PROTOCOL_VERSIONS = ["2025-03-26", "2025-06-18", "2025-11-25"]


def _schema(node: dict[str, Any]) -> agentcore.CfnGatewayTarget.SchemaDefinitionProperty:
    properties = {key: _schema(value) for key, value in node.get("properties", {}).items()}
    return agentcore.CfnGatewayTarget.SchemaDefinitionProperty(
        type=node["type"],
        description=node.get("description"),
        properties=properties or None,
        required=node.get("required"),
        items=_schema(node["items"]) if "items" in node else None,
    )


def load_tool_definitions() -> list[agentcore.CfnGatewayTarget.ToolDefinitionProperty]:
    """``src/tools/tool_schemas.json`` is the single source of truth for tool contracts."""
    definitions = json.loads(TOOL_SCHEMAS_FILE.read_text(encoding="utf-8"))
    return [
        agentcore.CfnGatewayTarget.ToolDefinitionProperty(
            name=tool["name"], description=tool["description"], input_schema=_schema(tool["inputSchema"])
        )
        for tool in definitions
    ]


class ToolGateway(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: StageConfig,
        auth: GatewayInboundAuth,
        tools_function: lambda_.IFunction,
    ) -> None:
        super().__init__(scope, construct_id)
        stack = cdk.Stack.of(self)

        self.role = iam.Role(
            self,
            "Role",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": stack.account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock-agentcore:{stack.region}:{stack.account}:gateway/*"
                    },
                },
            ),
            description="AgentCore Gateway role: invoke the support tools Lambda and evaluate Cedar policies",
        )
        tools_function.grant_invoke(self.role)
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="EvaluateCedarPolicies",
                actions=[
                    "bedrock-agentcore:AuthorizeAction",
                    "bedrock-agentcore:PartiallyAuthorizeActions",
                    "bedrock-agentcore:GetPolicyEngine",
                ],
                # Authorization is evaluated against both the engine and the gateway being protected.
                # The gateway ARN is matched by name prefix: referencing its own ARN here would create
                # a circular dependency (the gateway needs this role first).
                resources=[
                    f"arn:aws:bedrock-agentcore:{stack.region}:{stack.account}"
                    f":policy-engine/{config.policy_engine_name}*",
                    f"arn:aws:bedrock-agentcore:{stack.region}:{stack.account}"
                    f":gateway/{config.resource_prefix}-gateway-*",
                ],
            )
        )

        self.gateway = agentcore.CfnGateway(
            self,
            "Gateway",
            name=f"{config.resource_prefix}-gateway",
            description="Customer support business tools (MCP)",
            protocol_type="MCP",
            protocol_configuration=agentcore.CfnGateway.GatewayProtocolConfigurationProperty(
                mcp=agentcore.CfnGateway.MCPGatewayConfigurationProperty(
                    supported_versions=MCP_PROTOCOL_VERSIONS,
                    search_type="SEMANTIC",
                    instructions="Tools for looking up orders and customers and issuing refunds.",
                )
            ),
            authorizer_type="CUSTOM_JWT",
            authorizer_configuration=agentcore.CfnGateway.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=agentcore.CfnGateway.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=auth.discovery_url,
                    allowed_clients=[auth.client.user_pool_client_id],
                )
            ),
            role_arn=self.role.role_arn,
            # DEBUG returns detailed error messages to the MCP client; use None (generic errors) in prod.
            exception_level=None if config.is_production else "DEBUG",
        )
        self.gateway.node.add_dependency(self.role)
        if config.policy_engine_arn:
            self.gateway.add_property_override(
                "PolicyEngineConfiguration", {"Arn": config.policy_engine_arn, "Mode": "ENFORCE"}
            )

        self.target = agentcore.CfnGatewayTarget(
            self,
            "SupportToolsTarget",
            gateway_identifier=self.gateway.attr_gateway_identifier,
            name=GATEWAY_TARGET_NAME,
            description="Order status, customer profile and refunds",
            target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                    lambda_=agentcore.CfnGatewayTarget.McpLambdaTargetConfigurationProperty(
                        lambda_arn=tools_function.function_arn,
                        tool_schema=agentcore.CfnGatewayTarget.ToolSchemaProperty(
                            inline_payload=load_tool_definitions()
                        ),
                    )
                )
            ),
            credential_provider_configurations=[
                agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    credential_provider_type="GATEWAY_IAM_ROLE"
                )
            ],
        )
