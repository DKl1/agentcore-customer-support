"""Lambda function that implements the business tools behind the Gateway target."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from ..config import METRICS_NAMESPACE, TOOLS_SOURCE_DIR, StageConfig
from .data import DataStore

_BUNDLE_COMMAND = (
    "pip install -r requirements.txt -t /asset-output "
    "--platform manylinux2014_aarch64 --implementation cp --python-version 3.12 --only-binary=:all: "
    "&& cp -r support_tools /asset-output/"
)


class ToolsBackend(Construct):
    def __init__(self, scope: Construct, construct_id: str, *, config: StageConfig, data: DataStore) -> None:
        super().__init__(scope, construct_id)

        self.log_group = logs.LogGroup(
            self,
            "LogGroup",
            log_group_name=f"/aws/lambda/{config.resource_prefix}-tools",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=config.removal_policy,
        )

        role = iam.Role(
            self,
            "Role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Support tools Lambda: least-privilege access to the support tables only",
        )
        self.log_group.grant_write(role)

        self.function = lambda_.Function(
            self,
            "Function",
            function_name=f"{config.resource_prefix}-tools",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="support_tools.handler.lambda_handler",
            code=lambda_.Code.from_asset(
                str(TOOLS_SOURCE_DIR),
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    command=["bash", "-c", _BUNDLE_COMMAND],
                ),
            ),
            role=role,
            memory_size=512,
            # Downstream timeout (2.5s) + DynamoDB retries must fit well inside this.
            timeout=cdk.Duration.seconds(10),
            tracing=lambda_.Tracing.ACTIVE,
            log_group=self.log_group,
            logging_format=lambda_.LoggingFormat.JSON,
            environment={
                "CUSTOMERS_TABLE": data.customers.table_name,
                "ORDERS_TABLE": data.orders.table_name,
                "REFUNDS_TABLE": data.refunds.table_name,
                "IDEMPOTENCY_TABLE": data.idempotency.table_name,
                "MAX_REFUND_CENTS": str(config.max_refund_cents),
                "FAULT_INJECTION_ENABLED": str(config.enable_fault_injection).lower(),
                "DOWNSTREAM_TIMEOUT_SECONDS": "2.5",
                "POWERTOOLS_SERVICE_NAME": "support-tools",
                "POWERTOOLS_METRICS_NAMESPACE": METRICS_NAMESPACE,
                "POWERTOOLS_LOG_LEVEL": "INFO",
            },
        )

        # Explicit, action-level grants instead of grant_read_write_data (no Scan/Query/BatchWrite).
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadCustomers",
                actions=["dynamodb:GetItem"],
                resources=[data.customers.table_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadAndReserveOrders",
                actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
                resources=[data.orders.table_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteRefunds",
                actions=["dynamodb:PutItem"],
                resources=[data.refunds.table_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="IdempotencyRecords",
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                ],
                resources=[data.idempotency.table_arn],
            )
        )
