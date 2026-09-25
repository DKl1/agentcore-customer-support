"""E2E fixtures. Run with:  E2E=1 pytest -m e2e tests/e2e  (needs a deployed stack + AWS creds)."""

from __future__ import annotations

import json
import os
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "client"))

from _common import load_outputs  # noqa: E402
from gateway_probe import GatewayProbe  # noqa: E402
from support_client import AgentReply, SupportAgentClient  # noqa: E402

CUSTOMER = "CUST-1001"
RUN_ID = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
EVIDENCE_DIR = REPO_ROOT / "docs" / "evidence" / "runs" / RUN_ID


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("E2E") == "1":
        return
    skip = pytest.mark.skip(reason="set E2E=1 to run against a deployed stack")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def outputs() -> dict[str, str]:
    return load_outputs(os.environ.get("STAGE", "dev"))


@pytest.fixture(scope="session")
def agent(outputs: dict[str, str]) -> SupportAgentClient:
    return SupportAgentClient(outputs["RuntimeArn"], outputs["Region"])


@pytest.fixture(scope="session")
def probe(outputs: dict[str, str]) -> GatewayProbe:
    return GatewayProbe(outputs)


@pytest.fixture(scope="session")
def gateway_token(probe: GatewayProbe) -> str:
    return probe.access_token()


@pytest.fixture(scope="session")
def ddb(outputs: dict[str, str]) -> Any:
    return boto3.client("dynamodb", region_name=outputs["Region"])


@pytest.fixture
def new_session() -> Callable[[], str]:
    return lambda: SupportAgentClient.new_session_id(CUSTOMER)


@pytest.fixture
def operation_id() -> str:
    return f"operation-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def evidence(request: pytest.FixtureRequest) -> Callable[..., None]:
    """Persist a transcript of the scenario (prompt, reply, trace id) under docs/evidence/runs/."""
    entries: list[dict[str, Any]] = []

    def record(step: str, **data: Any) -> None:
        payload = {
            key: (value.raw if isinstance(value, AgentReply) else value) for key, value in data.items()
        }
        entries.append({"step": step, "at": datetime.now(UTC).isoformat(), **payload})

    yield record  # type: ignore[misc]

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / f"{request.node.name}.json").write_text(
        json.dumps({"test": request.node.nodeid, "run_id": RUN_ID, "steps": entries}, indent=2, default=str),
        encoding="utf-8",
    )


def refunds_for(ddb: Any, outputs: dict[str, str], **filters: Any) -> list[dict[str, Any]]:
    """Scan refunds (demo-sized table) matching exact attribute values."""
    from boto3.dynamodb.types import TypeDeserializer

    deserializer = TypeDeserializer()
    rows: list[dict[str, Any]] = []
    paginator = ddb.get_paginator("scan")
    for page in paginator.paginate(TableName=outputs["RefundsTable"]):
        for item in page["Items"]:
            row = {key: deserializer.deserialize(value) for key, value in item.items()}
            if all(row.get(key) == value for key, value in filters.items()):
                rows.append(row)
    return rows
