"""Collect observability and policy evidence after running the e2e scenarios.

Writes (non-secret) JSON into docs/evidence/:
  observability/<query>.json  results of the saved Logs Insights queries (last N hours)
  policy/gateway.json         gateway authorizer + policy engine attachment (mode)
  policy/policies.json        the Cedar statements active in the policy engine

Usage: python scripts/collect_evidence.py [--stage dev] [--hours 3]
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from _common import REPO_ROOT, client, load_outputs, log, paginate

EVIDENCE = REPO_ROOT / "docs" / "evidence"


def run_saved_queries(outputs: dict[str, str], hours: int) -> None:
    logs = client("logs", outputs)
    prefix = f"support-agent-{outputs['Stage']}/"
    definitions = logs.describe_query_definitions(queryDefinitionNamePrefix=prefix)["queryDefinitions"]
    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)
    target = EVIDENCE / "observability"
    target.mkdir(parents=True, exist_ok=True)

    for definition in sorted(definitions, key=lambda d: d["name"]):
        name = definition["name"].removeprefix(prefix)
        if "PASTE_" in definition["queryString"]:
            continue  # template query, needs a trace id
        try:
            query_id = logs.start_query(
                logGroupNames=definition["logGroupNames"],
                startTime=int(start.timestamp()),
                endTime=int(end.timestamp()),
                queryString=definition["queryString"],
            )["queryId"]
        except logs.exceptions.ResourceNotFoundException as error:
            log.warning("Skipping %s: %s (is Transaction Search enabled?)", name, error)
            continue
        result = _await_query(logs, query_id)
        rows = [{field["field"]: field["value"] for field in row} for row in result.get("results", [])]
        (target / f"{name}.json").write_text(
            json.dumps({"query": definition["queryString"], "window_hours": hours, "rows": rows}, indent=2),
            encoding="utf-8",
        )
        log.info("%-28s %d rows", name, len(rows))


def _await_query(logs: Any, query_id: str, timeout: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = logs.get_query_results(queryId=query_id)
        if result["status"] in {"Complete", "Failed", "Cancelled", "Timeout"}:
            return result
        time.sleep(1)
    return {"results": []}


def capture_policy_state(outputs: dict[str, str]) -> None:
    control = client("bedrock-agentcore-control", outputs)
    target = EVIDENCE / "policy"
    target.mkdir(parents=True, exist_ok=True)

    gateway = control.get_gateway(gatewayIdentifier=outputs["GatewayId"])
    summary = {
        key: gateway.get(key)
        for key in (
            "name",
            "status",
            "authorizerType",
            "authorizerConfiguration",
            "policyEngineConfiguration",
        )
    }
    (target / "gateway.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    engine_arn = (gateway.get("policyEngineConfiguration") or {}).get("arn")
    if not engine_arn:
        log.warning("No policy engine attached to the gateway!")
        return
    engine_id = engine_arn.rsplit("/", 1)[-1]
    policies = [
        {
            "name": item["name"],
            "status": item["status"],
            "statement": item["definition"]["cedar"]["statement"],
        }
        for item in paginate(control.list_policies, "policies", policyEngineId=engine_id)
    ]
    (target / "policies.json").write_text(json.dumps(policies, indent=2), encoding="utf-8")
    log.info("Captured %d policies (mode=%s)", len(policies), summary["policyEngineConfiguration"])

    runtime = control.get_agent_runtime(agentRuntimeId=outputs["RuntimeId"])
    hardening = {
        key: runtime.get(key) for key in ("status", "metadataConfiguration", "lifecycleConfiguration")
    }
    (target / "runtime.json").write_text(json.dumps(hardening, indent=2, default=str), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default="dev")
    parser.add_argument("--hours", type=int, default=3)
    args = parser.parse_args()
    outputs = load_outputs(args.stage)
    capture_policy_state(outputs)
    run_saved_queries(outputs, args.hours)


if __name__ == "__main__":
    main()
