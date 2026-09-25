"""Lambda entry point for the AgentCore Gateway Lambda target.

The Gateway invokes this function with the tool *arguments* as the event and the tool
name in ``context.client_context.custom["bedrockAgentCoreToolName"]`` (``<target>___<tool>``).

Contract: this function never raises to the Gateway. Every outcome is a JSON document::

    {"ok": true,  "tool": "...", "data": {...}}
    {"ok": false, "tool": "...", "error": {"code", "message", "http_status", "retryable"}}

so the agent can distinguish retryable infrastructure failures from business rejections.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache
from typing import Any

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit, single_metric
from botocore.config import Config
from pydantic import BaseModel, ValidationError

from .config import Settings
from .downstream import CarrierClient, PaymentProviderClient
from .errors import InternalError, InvalidParameters, ToolError, UnknownTool
from .idempotency import IdempotencyStore
from .models import GetCustomerRequest, GetOrderStatusRequest, RefundRequest
from .repository import SupportRepository, TableNames
from .services import CustomerService, OrderService, RefundService

TOOL_NAME_DELIMITER = "___"
METRICS_NAMESPACE = "CustomerSupportAgent"

logger = Logger(service="support-tools")
tracer = Tracer(service="support-tools")
metrics = Metrics(namespace=METRICS_NAMESPACE, service="support-tools")

_DDB_CONFIG = Config(retries={"mode": "adaptive", "max_attempts": 4}, connect_timeout=2, read_timeout=3)


class _Container:
    """Wires dependencies once per cold start (reused across warm invocations)."""

    def __init__(self, settings: Settings, ddb_client: Any) -> None:
        repository = SupportRepository(
            ddb_client,
            TableNames(
                customers=settings.customers_table,
                orders=settings.orders_table,
                refunds=settings.refunds_table,
                idempotency=settings.idempotency_table,
            ),
        )
        idempotency = IdempotencyStore(
            ddb_client,
            settings.idempotency_table,
            lock_seconds=settings.idempotency_lock_seconds,
            ttl_days=settings.idempotency_ttl_days,
        )
        self.orders = OrderService(
            repository,
            CarrierClient(
                timeout_seconds=settings.downstream_timeout_seconds,
                faults_enabled=settings.fault_injection_enabled,
            ),
        )
        self.customers = CustomerService(repository)
        self.refunds = RefundService(
            repository,
            idempotency,
            PaymentProviderClient(faults_enabled=settings.fault_injection_enabled),
            max_refund_cents=settings.max_refund_cents,
        )

    def routes(self) -> dict[str, tuple[type[BaseModel], Callable[[Any], dict[str, Any]]]]:
        return {
            "get_order_status": (GetOrderStatusRequest, self.orders.get_order_status),
            "get_customer": (GetCustomerRequest, self.customers.get_customer),
            "refund_customer": (RefundRequest, self.refunds.refund),
        }


@cache
def _container() -> _Container:
    return _Container(Settings.from_env(), boto3.client("dynamodb", config=_DDB_CONFIG))


def resolve_tool_name(context: Any) -> str:
    """``support-tools___get_order_status`` -> ``get_order_status``."""
    client_context = getattr(context, "client_context", None)
    custom = getattr(client_context, "custom", None) or {}
    qualified = custom.get("bedrockAgentCoreToolName", "")
    if not qualified:
        raise UnknownTool("Missing bedrockAgentCoreToolName in the Lambda client context")
    return qualified.split(TOOL_NAME_DELIMITER, 1)[-1]


def _validation_details(error: ValidationError) -> list[dict[str, str]]:
    # Echo field locations and messages, never the raw input values (may contain PII).
    return [
        {"field": ".".join(str(part) for part in err["loc"]), "error": err["msg"]} for err in error.errors()
    ]


def dispatch(tool_name: str, arguments: dict[str, Any], container: _Container) -> dict[str, Any]:
    routes = container.routes()
    if tool_name not in routes:
        raise UnknownTool(f"Unknown tool '{tool_name}'")
    model, operation = routes[tool_name]
    try:
        request = model.model_validate(arguments)
    except ValidationError as error:
        raise InvalidParameters(
            f"Invalid parameters for {tool_name}", details={"errors": _validation_details(error)}
        ) from error
    with tracer.provider.in_subsegment(f"## {tool_name}"):
        return operation(request)


def _record_error(tool_name: str, error: ToolError) -> None:
    tracer.put_annotation("error_code", error.code)
    tracer.put_annotation("retryable", error.retryable)
    with single_metric(
        name="ToolError", unit=MetricUnit.Count, value=1, namespace=METRICS_NAMESPACE
    ) as metric:
        metric.add_dimension("tool", tool_name)
        metric.add_dimension("error_code", error.code)


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler(capture_response=False)
@metrics.log_metrics
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    tool_name = "unknown"
    try:
        tool_name = resolve_tool_name(context)
        tracer.put_annotation("tool", tool_name)
        logger.append_keys(tool=tool_name, argument_keys=sorted((event or {}).keys()))
        metrics.add_metric(name="ToolInvocation", unit=MetricUnit.Count, value=1)

        data = dispatch(tool_name, event or {}, _container())
        logger.info("Tool call succeeded")
        return {"ok": True, "tool": tool_name, "data": data}
    except ToolError as error:
        log = logger.warning if error.retryable or error.http_status < 500 else logger.error
        log(
            "Tool call failed",
            extra={
                "error_code": error.code,
                "http_status": error.http_status,
                "retryable": error.retryable,
                "error_message": error.message,
            },
        )
        _record_error(tool_name, error)
        return {"ok": False, "tool": tool_name, "error": error.to_dict()}
    except Exception:
        logger.exception("Unhandled error in tool")
        internal = InternalError("Internal error while executing the tool")
        _record_error(tool_name, internal)
        return {"ok": False, "tool": tool_name, "error": internal.to_dict()}
