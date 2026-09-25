"""DynamoDB tables for the (simulated) order, customer and refund systems of record."""

from __future__ import annotations

from aws_cdk import aws_dynamodb as dynamodb
from constructs import Construct

from ..config import StageConfig


class DataStore(Construct):
    def __init__(self, scope: Construct, construct_id: str, *, config: StageConfig) -> None:
        super().__init__(scope, construct_id)

        def table(name: str, partition_key: str, ttl_attribute: str | None = None) -> dynamodb.TableV2:
            return dynamodb.TableV2(
                self,
                f"{name.title()}Table",
                table_name=f"{config.resource_prefix}-{name}",
                partition_key=dynamodb.Attribute(name=partition_key, type=dynamodb.AttributeType.STRING),
                billing=dynamodb.Billing.on_demand(),
                encryption=dynamodb.TableEncryptionV2.aws_managed_key(),
                point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                    point_in_time_recovery_enabled=True
                ),
                time_to_live_attribute=ttl_attribute,
                deletion_protection=config.is_production,
                removal_policy=config.removal_policy,
            )

        self.customers = table("customers", "customer_id")
        self.orders = table("orders", "order_id")
        self.refunds = table("refunds", "refund_id")
        self.idempotency = table("idempotency", "idempotency_key", ttl_attribute="expires_at")
