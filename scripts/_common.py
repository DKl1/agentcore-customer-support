"""Shared helpers for operator scripts. Credentials come from the operator's AWS session
(SSO / short-lived role); nothing is read from or written to files in the repo except the
non-secret CDK outputs in ``.deploy/outputs.json``."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import boto3

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUTS_FILE = REPO_ROOT / ".deploy" / "outputs.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scripts")


def load_outputs(stage: str = "dev", path: Path = OUTPUTS_FILE) -> dict[str, str]:
    if not path.exists():
        raise SystemExit(f"{path} not found — run `cdk deploy --outputs-file ../.deploy/outputs.json` first")
    stacks: dict[str, dict[str, str]] = json.loads(path.read_text(encoding="utf-8"))
    stack_name = f"CustomerSupportAgent-{stage}"
    if stack_name not in stacks:
        raise SystemExit(f"Stack {stack_name} not found in {path} (have: {', '.join(stacks)})")
    return stacks[stack_name]


def client(service: str, outputs: dict[str, str]) -> Any:
    return boto3.client(service, region_name=outputs["Region"])


def paginate(call: Callable[..., dict[str, Any]], items_key: str, **kwargs: Any) -> list[dict[str, Any]]:
    """Follow ``nextToken`` for control-plane list APIs that have no boto3 paginator."""
    items: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        page = call(**kwargs, **({"nextToken": token} if token else {}))
        items.extend(page.get(items_key, []))
        token = page.get("nextToken")
        if not token:
            return items


def wait_until(
    probe: Callable[[], str], *, ready: set[str], failed: set[str], what: str, timeout: float = 300
) -> str:
    deadline = time.monotonic() + timeout
    delay = 2.0
    while True:
        status = probe()
        if status in ready:
            return status
        if status in failed:
            raise SystemExit(f"{what} entered status {status}")
        if time.monotonic() > deadline:
            raise SystemExit(f"Timed out waiting for {what} (last status {status})")
        log.info("Waiting for %s (status=%s)", what, status)
        time.sleep(delay)
        delay = min(delay * 1.5, 15)
