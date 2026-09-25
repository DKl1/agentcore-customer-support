"""Tracing and structured logging.

Strands already emits OpenTelemetry spans for the agent loop (``invoke_agent``,
``execute_event_loop_cycle``, ``chat`` for LLM calls, ``execute_tool``). The AWS
OpenTelemetry distro (``opentelemetry-instrument`` in the Dockerfile) exports them to
CloudWatch / X-Ray, where AgentCore Observability shows them as Session -> Trace -> Span.

This module adds:
* ``gateway.tool_call`` spans nested under ``execute_tool``, carrying retry attempts, error
  codes, HTTP status and guard decisions (what the observability runbook searches for);
* one-line JSON log events that include ``trace_id``/``span_id``, so log lines and spans
  can be joined in CloudWatch Logs Insights.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace

tracer = trace.get_tracer("support_agent")

_logger = logging.getLogger("support_agent")
if not _logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def current_trace_ids() -> dict[str, str]:
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return {}
    return {"trace_id": format(context.trace_id, "032x"), "span_id": format(context.span_id, "016x")}


def xray_trace_id() -> str | None:
    """Trace id in X-Ray format (``1-xxxxxxxx-yyyy...``) for CloudWatch Transaction Search."""
    ids = current_trace_ids()
    if not ids:
        return None
    raw = ids["trace_id"]
    return f"1-{raw[:8]}-{raw[8:]}"


def log_event(event: str, level: int = logging.INFO, **fields: Any) -> None:
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "level": logging.getLevelName(level),
        "event": event,
        **current_trace_ids(),
        **{key: value for key, value in fields.items() if value is not None},
    }
    _logger.log(level, json.dumps(record, default=str))
