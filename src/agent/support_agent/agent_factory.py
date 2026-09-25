"""Builds the Strands agent for one invocation."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from botocore.config import Config
from strands import Agent
from strands.models import BedrockModel

from .config import AgentSettings
from .contracts import InvocationContext
from .prompts import render_system_prompt

# Throttling on Bedrock is retried by the SDK with adaptive backoff before surfacing an error.
_BEDROCK_CLIENT_CONFIG = Config(
    retries={"mode": "adaptive", "max_attempts": 6},
    connect_timeout=5,
    read_timeout=120,
)


def build_model(settings: AgentSettings) -> BedrockModel:
    model_config: dict[str, Any] = {"model_id": settings.model_id, "max_tokens": settings.max_output_tokens}
    if settings.guardrail_id and settings.guardrail_version:
        model_config |= {
            "guardrail_id": settings.guardrail_id,
            "guardrail_version": settings.guardrail_version,
            "guardrail_trace": "enabled",
        }
    return BedrockModel(
        region_name=settings.region, boto_client_config=_BEDROCK_CLIENT_CONFIG, **model_config
    )


def build_agent(
    settings: AgentSettings,
    context: InvocationContext,
    tools: Sequence[Any],
    session_manager: Any | None,
) -> Agent:
    return Agent(
        name="customer-support-agent",
        model=build_model(settings),
        system_prompt=render_system_prompt(context.customer_id),
        tools=list(tools),
        session_manager=session_manager,
        callback_handler=None,  # no stdout streaming inside the Runtime
        trace_attributes={
            "session.id": context.session_id,
            "user.id": context.customer_id,
            "operation.id": context.operation_id or "",
        },
    )
