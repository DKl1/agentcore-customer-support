"""AgentCore Runtime entry point (HTTP protocol, port 8080, ``/invocations``).

Request  (from the trusted backend, SigV4-authenticated by the Runtime):
    {"prompt": "...", "customer_id": "CUST-1001", "operation_id": "operation-123"?}
    runtimeSessionId = "<customer_id>-<uuid>"   (>= 33 chars, bound to the customer)

Response:
    {"response": "...", "session_id": "...", "trace_id": "1-...", "tool_calls": [...]}
    or {"error": {"code": "...", "message": "...", "retryable": bool}, "trace_id": "..."}
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from pydantic import ValidationError

from .agent_factory import build_agent
from .config import AgentSettings
from .contracts import InvalidRequest, InvocationContext, InvocationRequest
from .gateway import discover_business_tools, open_gateway_client
from .guards import ToolCallBudget, ToolCallLedger
from .identity import GatewayTokenProvider
from .memory import build_session_manager
from .retry import BackoffPolicy
from .telemetry import log_event, tracer, xray_trace_id
from .tool_proxy import GatewayToolProxy

app = BedrockAgentCoreApp()
settings = AgentSettings.from_env()  # fail fast at cold start on missing configuration
token_provider = GatewayTokenProvider(settings.gateway_credential_provider, settings.gateway_scopes)
backoff = BackoffPolicy(
    max_attempts=settings.retry.max_attempts,
    base_delay_seconds=settings.retry.base_delay_seconds,
    max_delay_seconds=settings.retry.max_delay_seconds,
)


def _error(code: str, message: str, *, retryable: bool) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "retryable": retryable}, "trace_id": xray_trace_id()}


async def _run_turn(context: InvocationContext, prompt: str, ledger: ToolCallLedger) -> str:
    access_token = await token_provider.get_token()
    gateway_client = open_gateway_client(settings.gateway_url, access_token)
    budget = ToolCallBudget(
        max_calls=settings.guards.max_tool_calls_per_turn,
        max_identical_calls=settings.guards.max_identical_calls,
    )
    with gateway_client:
        discovered = discover_business_tools(gateway_client, settings.allowed_tools)
        tools = [
            GatewayToolProxy(
                mcp_tool_name=tool.mcp_tool.name,
                public_name=name,
                tool_spec=tool.tool_spec,
                client=gateway_client,
                context=context,
                budget=budget,
                ledger=ledger,
                backoff=backoff,
                call_timeout_seconds=settings.retry.call_timeout_seconds,
            )
            for name, tool in discovered.items()
        ]
        session_manager = build_session_manager(
            memory_id=settings.memory_id,
            session_id=context.session_id,
            actor_id=context.customer_id,
            region=settings.region,
        )
        agent = build_agent(settings, context, tools, session_manager)
        result = await agent.invoke_async(prompt)
    return str(result).strip()


@app.entrypoint
async def invoke(payload: dict[str, Any], context: Any) -> dict[str, Any]:
    session_id = getattr(context, "session_id", None)

    with tracer.start_as_current_span("support_agent.invocation") as span:
        try:
            request = InvocationRequest.model_validate(payload)
            invocation = InvocationContext.create(
                request, session_id, enforce_session_binding=settings.guards.enforce_session_binding
            )
        except (ValidationError, InvalidRequest) as error:
            fields = (
                [".".join(map(str, e["loc"])) for e in error.errors()]
                if isinstance(error, ValidationError)
                else []
            )
            log_event(
                "invalid_request",
                logging.WARNING,
                session_id=session_id,
                invalid_fields=fields,
                detail=None if fields else str(error),
            )
            return _error("INVALID_REQUEST", "The request payload is invalid.", retryable=False)

        span.set_attributes({"session.id": invocation.session_id, "customer.id": invocation.customer_id})
        log_event(
            "invocation_started",
            session_id=invocation.session_id,
            customer_id=invocation.customer_id,
            operation_id=invocation.operation_id,
            prompt_chars=len(request.prompt),
        )

        ledger = ToolCallLedger()
        try:
            answer = await asyncio.wait_for(
                _run_turn(invocation, request.prompt, ledger), timeout=settings.guards.agent_timeout_seconds
            )
        except TimeoutError:
            log_event(
                "agent_timeout",
                logging.ERROR,
                session_id=invocation.session_id,
                timeout_seconds=settings.guards.agent_timeout_seconds,
                tool_calls=ledger.as_dicts(),
            )
            return _error("AGENT_TIMEOUT", "The assistant took too long to respond.", retryable=True)
        except Exception as error:
            span.record_exception(error)
            log_event(
                "invocation_failed",
                logging.ERROR,
                session_id=invocation.session_id,
                error=type(error).__name__,
                detail=str(error)[:500],
                tool_calls=ledger.as_dicts(),
            )
            return _error("INTERNAL_ERROR", "The assistant is temporarily unavailable.", retryable=True)

        log_event("invocation_completed", session_id=invocation.session_id, tool_calls=ledger.as_dicts())
        return {
            "response": answer,
            "session_id": invocation.session_id,
            "trace_id": xray_trace_id(),
            "tool_calls": ledger.as_dicts(),
        }


if __name__ == "__main__":
    app.run()
