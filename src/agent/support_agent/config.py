"""Agent configuration. Everything comes from environment variables injected by the
AgentCore Runtime definition (CDK); there are no credentials in here — the Runtime's
workload identity and execution role provide them at run time."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_ALLOWED_TOOLS = ("get_order_status", "get_customer", "refund_customer")


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True, slots=True)
class RetrySettings:
    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 4.0
    call_timeout_seconds: float = 15.0


@dataclass(frozen=True, slots=True)
class GuardSettings:
    max_tool_calls_per_turn: int = 8
    max_identical_calls: int = 2
    agent_timeout_seconds: float = 120.0
    enforce_session_binding: bool = True


@dataclass(frozen=True, slots=True)
class AgentSettings:
    region: str
    model_id: str
    gateway_url: str
    gateway_credential_provider: str
    gateway_scopes: tuple[str, ...]
    memory_id: str | None
    guardrail_id: str | None = None
    guardrail_version: str | None = None
    max_output_tokens: int = 4096
    allowed_tools: tuple[str, ...] = DEFAULT_ALLOWED_TOOLS
    retry: RetrySettings = field(default_factory=RetrySettings)
    guards: GuardSettings = field(default_factory=GuardSettings)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AgentSettings:
        env = dict(os.environ if env is None else env)
        return cls(
            region=env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "us-east-1",
            model_id=env["MODEL_ID"],
            gateway_url=env["GATEWAY_URL"],
            gateway_credential_provider=env["GATEWAY_CREDENTIAL_PROVIDER"],
            gateway_scopes=_csv(env.get("GATEWAY_SCOPES", "")),
            memory_id=env.get("MEMORY_ID") or None,
            guardrail_id=env.get("GUARDRAIL_ID") or None,
            guardrail_version=env.get("GUARDRAIL_VERSION") or None,
            max_output_tokens=int(env.get("MAX_OUTPUT_TOKENS", "4096")),
            allowed_tools=_csv(env.get("ALLOWED_TOOLS", ",".join(DEFAULT_ALLOWED_TOOLS))),
            retry=RetrySettings(
                max_attempts=int(env.get("TOOL_MAX_ATTEMPTS", "3")),
                base_delay_seconds=float(env.get("TOOL_BACKOFF_BASE_SECONDS", "0.5")),
                max_delay_seconds=float(env.get("TOOL_BACKOFF_MAX_SECONDS", "4.0")),
                call_timeout_seconds=float(env.get("TOOL_CALL_TIMEOUT_SECONDS", "15")),
            ),
            guards=GuardSettings(
                max_tool_calls_per_turn=int(env.get("MAX_TOOL_CALLS_PER_TURN", "8")),
                max_identical_calls=int(env.get("MAX_IDENTICAL_TOOL_CALLS", "2")),
                agent_timeout_seconds=float(env.get("AGENT_TIMEOUT_SECONDS", "120")),
                enforce_session_binding=_bool(env.get("ENFORCE_SESSION_BINDING", "true")),
            ),
        )
