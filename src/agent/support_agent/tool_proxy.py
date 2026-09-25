"""Gateway tool proxy — the agent-side reliability and trust layer around each MCP tool.

Tools are *discovered* dynamically from the AgentCore Gateway (``tools/list``) and *invoked*
through it (``tools/call``); this proxy wraps each discovered tool and adds, deterministically
and outside the model's control:

1. **Argument binding** — ``customer_id`` comes from the authenticated session and
   ``idempotency_key`` from the platform. Both are removed from the schema the model sees,
   so a prompt injection cannot redirect a call to another customer or forge a key.
2. **Loop guard** — per-turn tool-call budget and identical-call detection.
3. **Retries** — exponential backoff with jitter, only for errors classified as retryable.
   Policy denials and business rejections are never retried.
4. **Telemetry** — a ``gateway.tool_call`` span per call plus a JSON log event.

Authorization is *not* done here: the refund limit is enforced by the Cedar policy at the
Gateway (and again by the backend). This proxy is defence in depth, not the boundary.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Any, Protocol, cast

from opentelemetry.trace import Span, Status, StatusCode
from strands.types.tools import AgentTool, ToolResult, ToolSpec, ToolUse

from .contracts import InvocationContext
from .guards import ToolCallBudget, ToolCallLedger, ToolCallRecord
from .outcomes import ClassifiedResult, Outcome, classify
from .retry import BackoffPolicy
from .telemetry import log_event, tracer

try:  # Strands >= 1.10 wraps tool results in typed events.
    from strands.types._events import ToolResultEvent
except ImportError:  # pragma: no cover - older Strands yields the raw ToolResult
    ToolResultEvent = None  # type: ignore[assignment,misc]

REFUND_TOOL = "refund_customer"
PLATFORM_BOUND_ARGUMENTS = ("customer_id", "idempotency_key")


class McpToolCaller(Protocol):
    async def call_tool_async(
        self,
        tool_use_id: str,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: timedelta | None = None,
    ) -> Mapping[str, Any]: ...


class CustomerScopeViolation(Exception):
    """The model tried to act on a customer other than the authenticated one."""


def hide_platform_arguments(tool_spec: ToolSpec, public_name: str) -> ToolSpec:
    spec: dict[str, Any] = copy.deepcopy(dict(tool_spec))
    spec["name"] = public_name
    input_schema = spec.get("inputSchema", {})
    schema = input_schema.get("json", input_schema)
    properties = schema.get("properties", {})
    for argument in PLATFORM_BOUND_ARGUMENTS:
        properties.pop(argument, None)
    schema["required"] = [name for name in schema.get("required", []) if name not in PLATFORM_BOUND_ARGUMENTS]
    return spec  # type: ignore[return-value]


def bind_arguments(
    tool_name: str, model_input: dict[str, Any], context: InvocationContext, accepts_customer_id: bool
) -> dict[str, Any]:
    arguments = dict(model_input)
    supplied_customer = arguments.pop("customer_id", None)
    if supplied_customer is not None and supplied_customer != context.customer_id:
        raise CustomerScopeViolation(f"Tool call targeted customer {supplied_customer!r}")
    arguments.pop("idempotency_key", None)

    if accepts_customer_id:
        arguments["customer_id"] = context.customer_id
    if tool_name == REFUND_TOOL:
        arguments["idempotency_key"] = context.refund_idempotency_key(
            str(arguments.get("order_id", "")),
            arguments.get("amount_cents"),  # type: ignore[arg-type]
        )
    return arguments


def _error_result(tool_use_id: str, code: str, message: str) -> dict[str, Any]:
    return {"toolUseId": tool_use_id, "status": "error", "content": [{"text": f"{code}: {message}"}]}


class GatewayToolProxy(AgentTool):
    def __init__(
        self,
        *,
        mcp_tool_name: str,
        public_name: str,
        tool_spec: ToolSpec,
        client: McpToolCaller,
        context: InvocationContext,
        budget: ToolCallBudget,
        ledger: ToolCallLedger,
        backoff: BackoffPolicy,
        call_timeout_seconds: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__()
        original_schema = tool_spec.get("inputSchema", {})
        original_schema = original_schema.get("json", original_schema)
        self._accepts_customer_id = "customer_id" in original_schema.get("properties", {})
        self._mcp_name = mcp_tool_name
        self._public_name = public_name
        self._spec = hide_platform_arguments(tool_spec, public_name)
        self._client = client
        self._context = context
        self._budget = budget
        self._ledger = ledger
        self._backoff = backoff
        self._call_timeout = timedelta(seconds=call_timeout_seconds)
        self._sleep = sleep

    @property
    def tool_name(self) -> str:
        return self._public_name

    @property
    def tool_spec(self) -> ToolSpec:
        return self._spec

    @property
    def tool_type(self) -> str:
        return "mcp"

    async def stream(
        self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any
    ) -> AsyncGenerator[Any, None]:
        result = await self.execute(tool_use)
        yield ToolResultEvent(cast(ToolResult, result)) if ToolResultEvent is not None else result

    async def execute(self, tool_use: ToolUse) -> dict[str, Any]:
        tool_use_id = tool_use["toolUseId"]
        model_input = dict(tool_use.get("input") or {})
        started = time.perf_counter()

        with tracer.start_as_current_span("gateway.tool_call") as span:
            span.set_attributes(
                {
                    "gen_ai.tool.name": self._public_name,
                    "gen_ai.tool.call.id": tool_use_id,
                    "gateway.tool.name": self._mcp_name,
                    "session.id": self._context.session_id,
                    "customer.id": self._context.customer_id,
                }
            )

            violation = self._budget.check(self._public_name, model_input)
            if violation:
                return self._reject(
                    span, tool_use_id, violation.code, violation.message, "loop_guard_tripped", started
                )

            try:
                arguments = bind_arguments(
                    self._public_name, model_input, self._context, self._accepts_customer_id
                )
            except CustomerScopeViolation as error:
                return self._reject(
                    span,
                    tool_use_id,
                    "CUSTOMER_SCOPE_VIOLATION",
                    "You can only act on behalf of the authenticated customer.",
                    "customer_scope_violation",
                    started,
                    detail=str(error),
                )

            result, classified, attempts = await self._call_with_retries(span, tool_use_id, arguments)
            self._finish(span, classified, attempts, started)
            return self._annotate(result, classified, attempts, tool_use_id)

    async def _call_with_retries(
        self, span: Span, tool_use_id: str, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], ClassifiedResult, int]:
        attempt = 0
        while True:
            attempt += 1
            try:
                result: dict[str, Any] = dict(
                    await self._client.call_tool_async(
                        tool_use_id=tool_use_id,
                        name=self._mcp_name,
                        arguments=arguments,
                        read_timeout_seconds=self._call_timeout,
                    )
                )
            except Exception as error:  # transport failure (connection reset, client timeout, ...)
                result = _error_result(tool_use_id, "GATEWAY_TRANSIENT", f"connection error: {error}")

            classified = classify(result)
            span.add_event(
                "tool.attempt",
                {
                    "attempt": attempt,
                    "outcome": classified.outcome.value,
                    "error.code": classified.error_code or "",
                },
            )
            if classified.outcome is not Outcome.RETRYABLE_ERROR or not self._backoff.should_retry(attempt):
                return result, classified, attempt

            delay = self._backoff.delay_for(attempt)
            span.add_event("tool.retry_scheduled", {"attempt": attempt, "delay_seconds": round(delay, 3)})
            log_event(
                "tool_retry_scheduled",
                logging.WARNING,
                tool=self._public_name,
                attempt=attempt,
                delay_seconds=round(delay, 3),
                error_code=classified.error_code,
                http_status=classified.http_status,
            )
            await self._sleep(delay)

    def _finish(self, span: Span, classified: ClassifiedResult, attempts: int, started: float) -> None:
        latency_ms = int((time.perf_counter() - started) * 1000)
        span.set_attribute("tool.attempts", attempts)
        span.set_attribute("tool.outcome", classified.outcome.value)
        if classified.error_code:
            span.set_attribute("error.code", classified.error_code)
        if classified.http_status:
            span.set_attribute("http.response.status_code", classified.http_status)
        if classified.is_error:
            span.set_status(
                Status(StatusCode.ERROR, f"{classified.error_code}: {classified.message or ''}"[:256])
            )

        event = {
            Outcome.SUCCESS: "tool_call_succeeded",
            Outcome.POLICY_DENIED: "tool_call_denied_by_policy",
            Outcome.FATAL_ERROR: "tool_call_failed",
            Outcome.RETRYABLE_ERROR: "tool_retry_exhausted",
        }[classified.outcome]
        log_event(
            event,
            logging.INFO if classified.outcome is Outcome.SUCCESS else logging.WARNING,
            tool=self._public_name,
            outcome=classified.outcome.value,
            attempts=attempts,
            latency_ms=latency_ms,
            error_code=classified.error_code,
            http_status=classified.http_status,
            session_id=self._context.session_id,
            customer_id=self._context.customer_id,
        )
        self._ledger.add(
            ToolCallRecord(
                tool=self._public_name,
                outcome=classified.outcome.value,
                attempts=attempts,
                latency_ms=latency_ms,
                error_code=classified.error_code,
            )
        )

    def _reject(
        self,
        span: Span,
        tool_use_id: str,
        code: str,
        message: str,
        event: str,
        started: float,
        detail: str | None = None,
    ) -> dict[str, Any]:
        span.set_attribute("tool.outcome", "rejected_by_guard")
        span.set_attribute("error.code", code)
        span.set_status(Status(StatusCode.ERROR, code))
        log_event(
            event,
            logging.WARNING,
            tool=self._public_name,
            error_code=code,
            detail=detail,
            total_calls=self._budget.total_calls,
            session_id=self._context.session_id,
            customer_id=self._context.customer_id,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        self._ledger.add(ToolCallRecord(self._public_name, "rejected_by_guard", 0, latency_ms, code))
        return _error_result(tool_use_id, code, message)

    @staticmethod
    def _annotate(
        result: dict[str, Any], classified: ClassifiedResult, attempts: int, tool_use_id: str
    ) -> dict[str, Any]:
        """Add a platform note so the model reacts correctly instead of looping or retrying."""
        annotated = {**result, "toolUseId": tool_use_id, "content": list(result.get("content", []))}
        if classified.is_error:
            annotated["status"] = "error"
        note = None
        if classified.outcome is Outcome.POLICY_DENIED:
            note = (
                "PLATFORM NOTE: this action was DENIED by the authorization policy. Do not retry it or try "
                "to work around it. Tell the customer the request needs review by a human agent."
            )
        elif classified.outcome is Outcome.RETRYABLE_ERROR:
            note = (
                f"PLATFORM NOTE: the dependency is still failing after {attempts} attempts with backoff. "
                "Do not call this tool again in this turn; apologise and suggest trying again later."
            )
        if note:
            annotated["content"].append({"text": note})
        return annotated
