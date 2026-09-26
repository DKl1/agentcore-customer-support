"""Operational telemetry: log groups, metric filters, alarms, dashboard and saved
Logs Insights queries for the failure scenarios in docs/observability.md.

Signals:
* Agent JSON log events (``event`` field) in the Runtime log group -> metric filters.
* Lambda EMF metrics (``ToolError`` by ``tool``/``error_code``) from Powertools.
* OpenTelemetry spans in ``aws/spans`` (CloudWatch Transaction Search) for trace analysis.
"""

from __future__ import annotations

from dataclasses import dataclass

import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from constructs import Construct

from ..config import METRICS_NAMESPACE, StageConfig

SPANS_LOG_GROUP = "aws/spans"


@dataclass(frozen=True, slots=True)
class AgentSignal:
    event: str
    metric: str
    alarm_threshold: int | None  # None = dashboard only
    description: str


AGENT_SIGNALS = (
    AgentSignal("tool_call_denied_by_policy", "PolicyDenied", None, "Cedar policy denied a tool call"),
    AgentSignal(
        "customer_scope_violation", "CustomerScopeViolation", 1, "Model tried to act on another customer"
    ),
    AgentSignal("loop_guard_tripped", "LoopGuardTripped", 3, "Agent repeated tool calls / exceeded budget"),
    AgentSignal("tool_retry_scheduled", "ToolRetryScheduled", None, "Retryable tool failure, backing off"),
    AgentSignal("tool_retry_exhausted", "ToolRetryExhausted", 3, "Dependency still failing after retries"),
    AgentSignal("tool_call_failed", "ToolCallFailed", None, "Non-retryable tool error (e.g. invalid params)"),
    AgentSignal("agent_timeout", "AgentTimeout", 1, "Invocation exceeded the wall-clock budget"),
    AgentSignal("invocation_failed", "InvocationFailed", 1, "Unhandled error in the agent"),
    AgentSignal("gateway_tools_missing", "GatewayToolsMissing", 1, "Allowed tools missing from tools/list"),
    AgentSignal("invalid_request", "InvalidRequest", None, "Payload rejected by input validation"),
)

# name -> (log group key, query). Keys: "agent" (runtime logs), "tools" (Lambda logs), "spans".
SAVED_QUERIES: dict[str, tuple[str, str]] = {
    "1-tool-timeout": (
        "agent",
        "fields @timestamp, trace_id, span_id, tool, attempt, delay_seconds, error_code, latency_ms\n"
        "| filter error_code = 'DOWNSTREAM_TIMEOUT' or error_code = 'GATEWAY_TRANSIENT'\n"
        "| sort @timestamp desc | limit 50",
    ),
    "2-invalid-parameters": (
        "tools",
        "fields @timestamp, xray_trace_id, tool, error_code, error_message\n"
        "| filter error_code = 'INVALID_PARAMETERS'\n"
        "| sort @timestamp desc | limit 50",
    ),
    "3-wrong-tool-selection": (
        "spans",
        "fields @timestamp, traceId, name, attributes.gen_ai.tool.name as tool, attributes.session.id as session\n"
        "| filter name = 'gateway.tool_call' and attributes.gen_ai.tool.name = 'refund_customer'\n"
        "| sort @timestamp desc | limit 50",
    ),
    "4-http-500": (
        "agent",
        "fields @timestamp, trace_id, tool, attempts, error_code, http_status\n"
        "| filter http_status = 500\n"
        "| sort @timestamp desc | limit 50",
    ),
    "5-llm-loop": (
        "spans",
        "filter name = 'gateway.tool_call'\n"
        "| stats count(*) as tool_calls, count_distinct(attributes.gen_ai.tool.name) as distinct_tools by traceId\n"
        "| filter tool_calls >= 3\n"
        "| sort tool_calls desc | limit 20",
    ),
    "6-policy-denials": (
        "agent",
        "fields @timestamp, trace_id, session_id, customer_id, tool, error_code\n"
        "| filter event = 'tool_call_denied_by_policy'\n"
        "| sort @timestamp desc | limit 50",
    ),
    "7-trace-timeline": (
        "agent",
        "fields @timestamp, event, tool, attempt, outcome, error_code, http_status, latency_ms\n"
        "| filter trace_id = 'PASTE_OTEL_TRACE_ID_HERE'\n"
        "| sort @timestamp asc",
    ),
}


class Observability(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: StageConfig,
        runtime_log_group_name: str,
        tools_log_group: logs.ILogGroup,
        tools_function_name: str,
    ) -> None:
        super().__init__(scope, construct_id)

        # AgentCore Runtime creates its own log group when the Runtime is created, so it is referenced,
        # not owned (owning it fails with AlreadyExists). The stack makes this construct depend on the
        # Runtime so the group exists before metric filters attach; retention is set by
        # scripts/configure_runtime.py.
        self.agent_log_group = logs.LogGroup.from_log_group_name(
            self, "RuntimeLogGroup", runtime_log_group_name
        )
        self.alarm_topic = sns.Topic(self, "AlarmTopic", topic_name=f"{config.resource_prefix}-alarms")

        agent_metrics = {signal.metric: self._metric_filter(signal) for signal in AGENT_SIGNALS}
        alarms = [
            self._alarm(signal, agent_metrics[signal.metric])
            for signal in AGENT_SIGNALS
            if signal.alarm_threshold is not None
        ]
        tool_errors = cloudwatch.MathExpression(
            expression=f"SUM(SEARCH('{{{METRICS_NAMESPACE},error_code,service,tool}} MetricName=\"ToolError\"', "
            "'Sum', 60))",
            label="Tool errors (all codes)",
            period=cdk.Duration.minutes(1),
        )

        self._saved_queries(config, tools_log_group)
        self.dashboard = self._dashboard(config, agent_metrics, alarms, tool_errors, tools_function_name)

    def _metric_filter(self, signal: AgentSignal) -> cloudwatch.Metric:
        logs.MetricFilter(
            self,
            f"{signal.metric}Filter",
            log_group=self.agent_log_group,
            filter_pattern=logs.FilterPattern.string_value("$.event", "=", signal.event),
            metric_namespace=METRICS_NAMESPACE,
            metric_name=signal.metric,
            metric_value="1",
            default_value=0,
        )
        return cloudwatch.Metric(
            namespace=METRICS_NAMESPACE,
            metric_name=signal.metric,
            statistic="Sum",
            period=cdk.Duration.minutes(5),
        )

    def _alarm(self, signal: AgentSignal, metric: cloudwatch.Metric) -> cloudwatch.Alarm:
        alarm = metric.create_alarm(
            self,
            f"{signal.metric}Alarm",
            alarm_description=signal.description,
            threshold=signal.alarm_threshold or 1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        alarm.add_alarm_action(cw_actions.SnsAction(self.alarm_topic))
        return alarm

    def _saved_queries(self, config: StageConfig, tools_log_group: logs.ILogGroup) -> None:
        spans = logs.LogGroup.from_log_group_name(self, "SpansLogGroup", SPANS_LOG_GROUP)
        groups: dict[str, logs.ILogGroup] = {
            "agent": self.agent_log_group,
            "tools": tools_log_group,
            "spans": spans,
        }
        for name, (group_key, query) in SAVED_QUERIES.items():
            logs.CfnQueryDefinition(
                self,
                f"Query{name.split('-')[0]}",
                name=f"{config.resource_prefix}/{name}",
                query_string=query,
                log_group_names=[groups[group_key].log_group_name],
            )

    def _dashboard(
        self,
        config: StageConfig,
        agent_metrics: dict[str, cloudwatch.Metric],
        alarms: list[cloudwatch.Alarm],
        tool_errors: cloudwatch.IMetric,
        tools_function_name: str,
    ) -> cloudwatch.Dashboard:
        def lambda_metric(name: str, statistic: str) -> cloudwatch.Metric:
            return cloudwatch.Metric(
                namespace="AWS/Lambda",
                metric_name=name,
                dimensions_map={"FunctionName": tools_function_name},
                statistic=statistic,
                period=cdk.Duration.minutes(1),
            )

        dashboard = cloudwatch.Dashboard(
            self, "Dashboard", dashboard_name=f"{config.resource_prefix}-operations"
        )
        dashboard.add_widgets(
            cloudwatch.AlarmStatusWidget(title="Alarms", alarms=alarms, width=24, height=4),
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Security: policy denials & scope violations",
                left=[agent_metrics["PolicyDenied"], agent_metrics["CustomerScopeViolation"]],
                width=8,
            ),
            cloudwatch.GraphWidget(
                title="Reliability: retries, exhausted retries, timeouts",
                left=[
                    agent_metrics["ToolRetryScheduled"],
                    agent_metrics["ToolRetryExhausted"],
                    agent_metrics["AgentTimeout"],
                ],
                width=8,
            ),
            cloudwatch.GraphWidget(
                title="Agent behaviour: loop guard, failed calls, invalid requests",
                left=[
                    agent_metrics["LoopGuardTripped"],
                    agent_metrics["ToolCallFailed"],
                    agent_metrics["InvalidRequest"],
                ],
                width=8,
            ),
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(title="Tool errors by code (Lambda EMF)", left=[tool_errors], width=8),
            cloudwatch.GraphWidget(
                title="Tools Lambda duration (p50 / p99)",
                left=[lambda_metric("Duration", "p50"), lambda_metric("Duration", "p99")],
                width=8,
            ),
            cloudwatch.GraphWidget(
                title="Tools Lambda invocations / throttles",
                left=[lambda_metric("Invocations", "Sum"), lambda_metric("Throttles", "Sum")],
                width=8,
            ),
        )
        return dashboard
