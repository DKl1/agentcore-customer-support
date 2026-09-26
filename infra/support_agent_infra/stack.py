"""Composition root: wires the constructs together and exports what scripts/tests need."""

from __future__ import annotations

import aws_cdk as cdk
from constructs import Construct

from .config import GATEWAY_TARGET_NAME, StageConfig
from .constructs.data import DataStore
from .constructs.gateway import ToolGateway
from .constructs.guardrail import InputGuardrail
from .constructs.identity import GatewayInboundAuth
from .constructs.memory import AgentMemory
from .constructs.observability import Observability
from .constructs.runtime import AgentRuntime
from .constructs.tools import ToolsBackend


class CustomerSupportAgentStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, *, config: StageConfig, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)

        data = DataStore(self, "Data", config=config)
        tools = ToolsBackend(self, "Tools", config=config, data=data)
        auth = GatewayInboundAuth(self, "GatewayAuth", config=config)
        gateway = ToolGateway(self, "Gateway", config=config, auth=auth, tools_function=tools.function)
        memory = AgentMemory(self, "Memory", config=config)
        guardrail = InputGuardrail(self, "Guardrail", config=config) if config.enable_guardrail else None
        runtime = AgentRuntime(
            self, "Runtime", config=config, gateway=gateway, auth=auth, memory=memory, guardrail=guardrail
        )
        observability = Observability(
            self,
            "Observability",
            config=config,
            runtime_log_group_name=runtime.log_group_name,
            tools_log_group=tools.log_group,
            tools_function_name=tools.function.function_name,
        )
        # The Runtime creates its log group on creation; metric filters and queries must come after it.
        observability.node.add_dependency(runtime.runtime)

        outputs = {
            "Stage": config.stage,
            "Region": self.region,
            "RuntimeArn": runtime.runtime.attr_agent_runtime_arn,
            "RuntimeId": runtime.runtime.attr_agent_runtime_id,
            "RuntimeLogGroup": runtime.log_group_name,
            "GatewayId": gateway.gateway.attr_gateway_identifier,
            "GatewayArn": gateway.gateway.attr_gateway_arn,
            "GatewayUrl": gateway.gateway.attr_gateway_url,
            "GatewayTargetName": GATEWAY_TARGET_NAME,
            "MemoryId": memory.memory.attr_memory_id,
            "UserPoolId": auth.user_pool.user_pool_id,
            "GatewayClientId": auth.client.user_pool_client_id,
            "GatewayDiscoveryUrl": auth.discovery_url,
            "GatewayTokenScope": auth.scope,
            "CredentialProviderName": config.credential_provider_name,
            "PolicyEngineName": config.policy_engine_name,
            "ToolsFunctionName": tools.function.function_name,
            "ToolsLogGroup": tools.log_group.log_group_name,
            "CustomersTable": data.customers.table_name,
            "OrdersTable": data.orders.table_name,
            "RefundsTable": data.refunds.table_name,
            "IdempotencyTable": data.idempotency.table_name,
            "AlarmTopicArn": observability.alarm_topic.topic_arn,
            "DashboardName": observability.dashboard.dashboard_name,
            "MaxRefundCents": str(config.max_refund_cents),
        }
        for key, value in outputs.items():
            cdk.CfnOutput(self, key, value=value)
