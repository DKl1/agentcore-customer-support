"""DynamoDB data access. All money values are integer cents (no floats anywhere)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from .errors import DownstreamUnavailable

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()

_THROTTLING_CODES = {
    "ProvisionedThroughputExceededException",
    "ThrottlingException",
    "RequestLimitExceeded",
    "InternalServerError",
}


def to_item(data: dict[str, Any]) -> dict[str, Any]:
    return {key: _serializer.serialize(value) for key, value in data.items() if value is not None}


def from_item(item: dict[str, Any]) -> dict[str, Any]:
    return {key: _normalize(_deserializer.deserialize(value)) for key, value in item.items()}


def _normalize(value: Any) -> Any:
    """DynamoDB returns numbers as Decimal (and sets as set); make values JSON-serialisable."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _normalize(inner) for key, inner in value.items()}
    if isinstance(value, list | set):
        return [_normalize(inner) for inner in value]
    return value


def error_code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def raise_if_throttled(error: ClientError) -> None:
    """Translate DynamoDB throttling (after SDK retries are exhausted) into a retryable tool error."""
    if error_code(error) in _THROTTLING_CODES:
        raise DownstreamUnavailable("Data store is throttling requests", http_status=503) from error


@dataclass(frozen=True, slots=True)
class TableNames:
    customers: str
    orders: str
    refunds: str
    idempotency: str


class SupportRepository:
    def __init__(self, client: Any, tables: TableNames) -> None:
        self._ddb = client
        self._tables = tables

    def get_customer(self, customer_id: str) -> dict[str, Any] | None:
        return self._get(self._tables.customers, {"customer_id": customer_id})

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        return self._get(self._tables.orders, {"order_id": order_id})

    def reserve_refund(self, *, order_id: str, customer_id: str, amount_cents: int, total_cents: int) -> bool:
        """Atomically add ``amount_cents`` to the order's refunded total.

        Returns False when the order no longer has enough refundable balance (e.g. a
        concurrent refund with a different idempotency key won the race).
        """
        try:
            self._ddb.update_item(
                TableName=self._tables.orders,
                Key=to_item({"order_id": order_id}),
                UpdateExpression="SET refunded_cents = if_not_exists(refunded_cents, :zero) + :amount",
                ConditionExpression=(
                    "customer_id = :customer AND "
                    "(attribute_not_exists(refunded_cents) OR refunded_cents <= :max_prior)"
                ),
                ExpressionAttributeValues=to_item(
                    {
                        ":zero": 0,
                        ":amount": amount_cents,
                        ":customer": customer_id,
                        ":max_prior": total_cents - amount_cents,
                    }
                ),
            )
            return True
        except ClientError as error:
            if error_code(error) == "ConditionalCheckFailedException":
                return False
            raise_if_throttled(error)
            raise

    def release_refund_reservation(self, *, order_id: str, amount_cents: int) -> None:
        """Compensating action when the payment provider fails after the reservation."""
        self._ddb.update_item(
            TableName=self._tables.orders,
            Key=to_item({"order_id": order_id}),
            UpdateExpression="SET refunded_cents = refunded_cents - :amount",
            ConditionExpression="refunded_cents >= :amount",
            ExpressionAttributeValues=to_item({":amount": amount_cents}),
        )

    def commit_refund(self, *, refund: dict[str, Any], idempotency_pk: str, result: dict[str, Any]) -> None:
        """Persist the refund and mark the idempotency record COMPLETED in one transaction."""
        self._ddb.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": self._tables.refunds,
                        "Item": to_item(refund),
                        "ConditionExpression": "attribute_not_exists(refund_id)",
                    }
                },
                {
                    "Update": {
                        "TableName": self._tables.idempotency,
                        "Key": to_item({"idempotency_key": idempotency_pk}),
                        "UpdateExpression": "SET #status = :completed, #result = :result",
                        "ConditionExpression": "#status = :in_progress",
                        "ExpressionAttributeNames": {"#status": "status", "#result": "result"},
                        "ExpressionAttributeValues": to_item(
                            {
                                ":completed": "COMPLETED",
                                ":in_progress": "IN_PROGRESS",
                                ":result": json.dumps(result, sort_keys=True),
                            }
                        ),
                    }
                },
            ]
        )

    def _get(self, table: str, key: dict[str, Any]) -> dict[str, Any] | None:
        try:
            response = self._ddb.get_item(TableName=table, Key=to_item(key), ConsistentRead=True)
        except ClientError as error:
            raise_if_throttled(error)
            raise
        item = response.get("Item")
        return from_item(item) if item else None
