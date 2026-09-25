"""AgentCore Runtime hosting the Strands agent container.

Execution role = least privilege, scoped to:
* one Bedrock model (inference profile + its foundation models) and optionally one guardrail,
* one Memory resource (data-plane event + retrieval actions only),
* AgentCore Identity: this Runtime's workload identity and one OAuth2 credential provider,
* its own log group, X-Ray and CloudWatch metrics (``bedrock-agentcore`` namespace only),
* pulling its own ECR image.

Inbound: IAM SigV4 (only principals granted ``bedrock-agentcore:InvokeAgentRuntime`` on this
runtime — i.e. the trusted backend — can invoke it).
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from constructs import Construct

from ..config import AGENT_SOURCE_DIR, StageConfig
from .gateway import ToolGateway
from .guardrail import InputGuardrail
from .identity import GatewayInboundAuth
from .memory import AgentMemory

IDLE_SESSION_TIMEOUT_SECONDS = 900  # 15 min — interactive support chat
MAX_SESSION_LIFETIME_SECONDS = 8 * 3600


def _foundation_model_name(model_id: str) -> str:
    """``us.anthropic.claude-opus-5`` -> ``anthropic.claude-opus-5`` (strip the geo prefix)."""
    parts = model_id.split(".")
    return ".".join(parts[1:]) if parts[0] in {"us", "eu", "apac", "global", "jp", "au"} else model_id


class AgentRuntime(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: StageConfig,
        gateway: ToolGateway,
        auth: GatewayInboundAuth,
        memory: AgentMemory,
        guardrail: InputGuardrail | None,
    ) -> None:
        super().__init__(scope, construct_id)
        stack = cdk.Stack.of(self)
        region, account = stack.region, stack.account
        runtime_name = config.agentcore_prefix

        image = ecr_assets.DockerImageAsset(
            self, "Image", directory=str(AGENT_SOURCE_DIR), platform=ecr_assets.Platform.LINUX_ARM64
        )

        self.role = iam.Role(
            self,
            "ExecutionRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account}:*"},
                },
            ),
            description="AgentCore Runtime execution role for the customer support agent",
        )
        image.repository.grant_pull(self.role)
        self._grant_model_access(config, guardrail, region, account)
        self._grant_memory_access(memory)
        self._grant_identity_access(config, runtime_name, region, account)
        self._grant_observability(region, account)

        environment = {
            "MODEL_ID": config.model_id,
            "GATEWAY_URL": gateway.gateway.attr_gateway_url,
            "GATEWAY_CREDENTIAL_PROVIDER": config.credential_provider_name,
            "GATEWAY_SCOPES": auth.scope,
            "MEMORY_ID": memory.memory.attr_memory_id,
            "ALLOWED_TOOLS": "get_order_status,get_customer,refund_customer",
            "TOOL_MAX_ATTEMPTS": "3",
            "MAX_TOOL_CALLS_PER_TURN": "8",
            "MAX_IDENTICAL_TOOL_CALLS": "2",
            "AGENT_TIMEOUT_SECONDS": "120",
            # OpenTelemetry -> CloudWatch (AgentCore Observability / GenAI Observability dashboards)
            "AGENT_OBSERVABILITY_ENABLED": "true",
            "OTEL_PYTHON_DISTRO": "aws_distro",
            "OTEL_PYTHON_CONFIGURATOR": "aws_configurator",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_TRACES_EXPORTER": "otlp",
            "OTEL_RESOURCE_ATTRIBUTES": f"service.name={runtime_name},deployment.environment={config.stage}",
        }
        if guardrail:
            environment["GUARDRAIL_ID"] = guardrail.guardrail.attr_guardrail_id
            environment["GUARDRAIL_VERSION"] = guardrail.version.attr_version

        self.runtime = agentcore.CfnRuntime(
            self,
            "Runtime",
            agent_runtime_name=runtime_name,
            description="Customer Support Agent (Strands) — orders, customers, refunds",
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=image.image_uri
                )
            ),
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(network_mode="PUBLIC"),
            protocol_configuration="HTTP",
            role_arn=self.role.role_arn,
            environment_variables=environment,
        )
        # MMDSv2 (metadataConfiguration.requireMMDSV2) is not in the CloudFormation schema yet; it is
        # enforced by scripts/configure_runtime.py, which `make deploy` runs after every deployment.
        self.runtime.add_property_override(
            "LifecycleConfiguration",
            {
                "IdleRuntimeSessionTimeout": IDLE_SESSION_TIMEOUT_SECONDS,
                "MaxLifetime": MAX_SESSION_LIFETIME_SECONDS,
            },
        )
        self.runtime.node.add_dependency(self.role)

        self.log_group_name = f"/aws/bedrock-agentcore/runtimes/{self.runtime.attr_agent_runtime_id}-DEFAULT"

    def _grant_model_access(
        self, config: StageConfig, guardrail: InputGuardrail | None, region: str, account: str
    ) -> None:
        foundation_model = _foundation_model_name(config.model_id)
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeConfiguredModelOnly",
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[
                    f"arn:aws:bedrock:{region}:{account}:inference-profile/{config.model_id}",
                    # Cross-region inference profiles route to the foundation model in several regions.
                    f"arn:aws:bedrock:*::foundation-model/{foundation_model}*",
                ],
            )
        )
        if guardrail:
            self.role.add_to_policy(
                iam.PolicyStatement(
                    sid="ApplyGuardrail",
                    actions=["bedrock:ApplyGuardrail"],
                    resources=[guardrail.guardrail.attr_guardrail_arn],
                )
            )

    def _grant_memory_access(self, memory: AgentMemory) -> None:
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="UseSupportMemoryOnly",
                actions=[
                    "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:GetEvent",
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:ListSessions",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                    "bedrock-agentcore:ListMemoryRecords",
                    "bedrock-agentcore:GetMemoryRecord",
                ],
                resources=[memory.memory.attr_memory_arn],
            )
        )

    def _grant_identity_access(
        self, config: StageConfig, runtime_name: str, region: str, account: str
    ) -> None:
        directory = f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default"
        token_vault = f"arn:aws:bedrock-agentcore:{region}:{account}:token-vault/default"
        provider = config.credential_provider_name
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="OwnWorkloadIdentityToken",
                actions=[
                    "bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                    "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                ],
                resources=[directory, f"{directory}/workload-identity/{runtime_name}-*"],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="GatewayM2MTokenFromVault",
                actions=["bedrock-agentcore:GetResourceOauth2Token"],
                resources=[
                    token_vault,
                    f"{token_vault}/oauth2credentialprovider/{provider}",
                    directory,
                    f"{directory}/workload-identity/{runtime_name}-*",
                ],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadProviderClientSecret",
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:bedrock-agentcore-identity!default/oauth2/{provider}*"
                ],
            )
        )

    def _grant_observability(self, region: str, account: str) -> None:
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="RuntimeLogs",
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                ],
                resources=[
                    f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*",
                    f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*",
                ],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DescribeLogGroups",
                actions=["logs:DescribeLogGroups"],
                resources=[f"arn:aws:logs:{region}:{account}:log-group:*"],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="Tracing",
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                ],
                resources=["*"],  # X-Ray does not support resource-level permissions
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="AgentMetrics",
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}},
            )
        )
