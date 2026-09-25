from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from moto import mock_aws
from support_tools.downstream import CarrierClient, PaymentProviderClient
from support_tools.idempotency import IdempotencyStore
from support_tools.repository import SupportRepository, TableNames, to_item
from support_tools.services import CustomerService, OrderService, RefundService

SEED = json.loads((Path(__file__).resolve().parents[3] / "data" / "seed.json").read_text(encoding="utf-8"))
TABLES = TableNames(customers="customers", orders="orders", refunds="refunds", idempotency="idempotency")
MAX_REFUND_CENTS = 100_000


@pytest.fixture
def ddb() -> Iterator[Any]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        for table, key in (
            (TABLES.customers, "customer_id"),
            (TABLES.orders, "order_id"),
            (TABLES.refunds, "refund_id"),
            (TABLES.idempotency, "idempotency_key"),
        ):
            client.create_table(
                TableName=table,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        for customer in SEED["customers"]:
            client.put_item(TableName=TABLES.customers, Item=to_item(customer))
        for order in SEED["orders"]:
            client.put_item(TableName=TABLES.orders, Item=to_item(order))
        yield client


@pytest.fixture
def repository(ddb: Any) -> SupportRepository:
    return SupportRepository(ddb, TABLES)


@pytest.fixture
def idempotency(ddb: Any) -> IdempotencyStore:
    return IdempotencyStore(ddb, TABLES.idempotency, lock_seconds=60, ttl_days=7)


@pytest.fixture
def refunds(repository: SupportRepository, idempotency: IdempotencyStore) -> RefundService:
    return RefundService(
        repository, idempotency, PaymentProviderClient(faults_enabled=True), max_refund_cents=MAX_REFUND_CENTS
    )


@pytest.fixture
def orders(repository: SupportRepository) -> OrderService:
    return OrderService(
        repository, CarrierClient(timeout_seconds=0.01, faults_enabled=True, sleep=lambda _: None)
    )


@pytest.fixture
def customers(repository: SupportRepository) -> CustomerService:
    return CustomerService(repository)


def lambda_context(tool: str) -> SimpleNamespace:
    return SimpleNamespace(
        function_name="support-tools",
        memory_limit_in_mb=512,
        invoked_function_arn="arn:aws:lambda:us-east-1:123456789012:function:support-tools",
        aws_request_id="req-1",
        client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": f"support-tools___{tool}"}),
    )
