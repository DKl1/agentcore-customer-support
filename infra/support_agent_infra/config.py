"""Stage configuration resolved from CDK context (``cdk.json`` defaults, ``-c key=value`` overrides)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import aws_cdk as cdk

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_SOURCE_DIR = REPO_ROOT / "src" / "agent"
TOOLS_SOURCE_DIR = REPO_ROOT / "src" / "tools"
TOOL_SCHEMAS_FILE = TOOLS_SOURCE_DIR / "tool_schemas.json"

# Shared names — also referenced by scripts/ and policies/ (keep in sync).
GATEWAY_TARGET_NAME = "support-tools"
TOKEN_RESOURCE_SERVER_ID = "support-gateway"  # noqa: S105 - OAuth resource server id, not a secret
TOKEN_SCOPE = "tools.invoke"  # noqa: S105 - OAuth scope name, not a secret
METRICS_NAMESPACE = "CustomerSupportAgent"


def _as_bool(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True, slots=True)
class StageConfig:
    stage: str
    model_id: str
    max_refund_cents: int
    enable_fault_injection: bool
    enable_guardrail: bool
    policy_engine_arn: str | None

    @classmethod
    def from_context(cls, scope: cdk.App) -> StageConfig:
        stage = str(scope.node.try_get_context("stage") or "dev")
        if not re.fullmatch(r"[a-z][a-z0-9]{1,9}", stage):
            raise ValueError("stage must be 2-10 lowercase alphanumerics, starting with a letter")
        config = cls(
            stage=stage,
            model_id=str(scope.node.try_get_context("modelId")),
            max_refund_cents=int(scope.node.try_get_context("maxRefundCents") or 100_000),
            enable_fault_injection=_as_bool(scope.node.try_get_context("enableFaultInjection")),
            enable_guardrail=_as_bool(scope.node.try_get_context("enableGuardrail")),
            policy_engine_arn=scope.node.try_get_context("policyEngineArn") or None,
        )
        if config.is_production and config.enable_fault_injection:
            raise ValueError("Fault injection must never be enabled in the prod stage")
        return config

    @property
    def is_production(self) -> bool:
        return self.stage == "prod"

    @property
    def removal_policy(self) -> cdk.RemovalPolicy:
        return cdk.RemovalPolicy.RETAIN if self.is_production else cdk.RemovalPolicy.DESTROY

    @property
    def resource_prefix(self) -> str:
        """For resources that allow hyphens (Lambda, DynamoDB, Cognito, Gateway)."""
        return f"support-agent-{self.stage}"

    @property
    def agentcore_prefix(self) -> str:
        """For AgentCore Runtime / Memory names (``[a-zA-Z][a-zA-Z0-9_]*``)."""
        return f"support_agent_{self.stage}"

    @property
    def policy_engine_name(self) -> str:
        return f"{self.agentcore_prefix}_policies"

    @property
    def credential_provider_name(self) -> str:
        return f"{self.resource_prefix}-gateway-m2m"
