"""Provision AgentCore Policy (Cedar) for the Gateway and attach it in ENFORCE mode.

Steps (idempotent — safe to re-run after every deploy):
  1. ensure the policy engine exists and is ACTIVE;
  2. ensure the Gateway references it with the requested mode;
  3. render policies/support_tools.cedar and create/update one Policy per statement
     (validated server-side with FAIL_ON_ANY_FINDINGS against the Gateway's tool schema);
  4. print the engine ARN so it can be pinned in CDK (``-c policyEngineArn=...``),
     which prevents a later ``cdk deploy`` from detaching the engine.

Usage: python scripts/configure_policy.py [--stage dev] [--mode ENFORCE|LOG_ONLY]
"""

from __future__ import annotations

import argparse
import re
from typing import Any

from _common import REPO_ROOT, client, load_outputs, log, paginate, wait_until

POLICY_FILE = REPO_ROOT / "policies" / "support_tools.cedar"
STATEMENT_SEPARATOR = re.compile(r"^// ---\s*$", re.MULTILINE)
ID_ANNOTATION = re.compile(r'@id\("([a-z0-9-]+)"\)')
# Everything UpdateGateway accepts besides the identifier and the policy engine (full replace semantics).
_GATEWAY_FIELDS = (
    "name",
    "description",
    "roleArn",
    "protocolType",
    "protocolConfiguration",
    "authorizerType",
    "authorizerConfiguration",
    "kmsKeyArn",
    "customTransformConfiguration",
    "interceptorConfigurations",
    "exceptionLevel",
    "wafConfiguration",
)
_FAILED = {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}


def render_policies(outputs: dict[str, str]) -> dict[str, str]:
    """Returns {policy_name: cedar_statement}."""
    text = (
        POLICY_FILE.read_text(encoding="utf-8")
        .replace("{{GATEWAY_ARN}}", outputs["GatewayArn"])
        .replace("{{TARGET}}", outputs["GatewayTargetName"])
        .replace("{{MAX_REFUND}}", outputs["MaxRefundCents"])
    )
    if "{{" in text:
        raise SystemExit("Unrendered placeholder left in the Cedar policy")
    policies: dict[str, str] = {}
    for chunk in STATEMENT_SEPARATOR.split(text):
        match = ID_ANNOTATION.search(chunk)
        if match:
            policies[match.group(1).replace("-", "_")] = chunk[
                match.start() :
            ].strip()  # drop leading comments
    return policies


def ensure_policy_engine(control: Any, name: str) -> tuple[str, str]:
    for engine in paginate(control.list_policy_engines, "policyEngines"):
        if engine["name"] == name:
            log.info("Policy engine %s exists (%s, %s)", name, engine["policyEngineId"], engine["status"])
            return engine["policyEngineId"], engine["policyEngineArn"]
    created = control.create_policy_engine(name=name, description="Customer support agent tool authorization")
    engine_id = created["policyEngineId"]
    wait_until(
        lambda: control.get_policy_engine(policyEngineId=engine_id)["status"],
        ready={"ACTIVE"},
        failed=_FAILED,
        what=f"policy engine {name}",
    )
    log.info("Created policy engine %s", engine_id)
    return engine_id, created["policyEngineArn"]


def ensure_gateway_attachment(control: Any, gateway_id: str, engine_arn: str, mode: str) -> None:
    gateway = control.get_gateway(gatewayIdentifier=gateway_id)
    current = gateway.get("policyEngineConfiguration") or {}
    if current.get("arn") == engine_arn and current.get("mode") == mode:
        log.info("Gateway already uses the policy engine in %s mode", mode)
        return
    fields = {field: gateway[field] for field in _GATEWAY_FIELDS if gateway.get(field) is not None}
    control.update_gateway(
        gatewayIdentifier=gateway_id, policyEngineConfiguration={"arn": engine_arn, "mode": mode}, **fields
    )
    wait_until(
        lambda: control.get_gateway(gatewayIdentifier=gateway_id)["status"],
        ready={"READY"},
        failed={"FAILED", "UPDATE_UNSUCCESSFUL"},
        what="gateway update",
    )
    log.info("Attached policy engine to gateway in %s mode", mode)


def upsert_policies(control: Any, engine_id: str, policies: dict[str, str]) -> None:
    existing = {
        p["name"]: p["policyId"]
        for p in paginate(control.list_policies, "policies", policyEngineId=engine_id)
    }

    for name, statement in policies.items():
        definition = {"cedar": {"statement": statement}}
        if name in existing:
            policy_id = existing[name]
            control.update_policy(
                policyEngineId=engine_id,
                policyId=policy_id,
                definition=definition,
                validationMode="FAIL_ON_ANY_FINDINGS",
            )
        else:
            policy_id = control.create_policy(
                policyEngineId=engine_id,
                name=name,
                definition=definition,
                description=f"Customer support agent: {name}",
                validationMode="FAIL_ON_ANY_FINDINGS",
            )["policyId"]
        status = wait_until(
            lambda pid=policy_id: control.get_policy(policyEngineId=engine_id, policyId=pid)["status"],
            ready={"ACTIVE"} | _FAILED,
            failed=set(),
            what=f"policy {name}",
        )
        if status in _FAILED:
            reasons = control.get_policy(policyEngineId=engine_id, policyId=policy_id).get("statusReasons")
            raise SystemExit(f"Policy {name} failed validation: {reasons}")
        log.info("Policy %s is ACTIVE", name)

    for name in sorted(set(existing) - set(policies)):
        control.delete_policy(policyEngineId=engine_id, policyId=existing[name])
        log.info("Deleted stale policy %s", name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default="dev")
    parser.add_argument("--mode", choices=("ENFORCE", "LOG_ONLY"), default="ENFORCE")
    args = parser.parse_args()

    outputs = load_outputs(args.stage)
    control = client("bedrock-agentcore-control", outputs)

    engine_id, engine_arn = ensure_policy_engine(control, outputs["PolicyEngineName"])
    ensure_gateway_attachment(control, outputs["GatewayId"], engine_arn, args.mode)
    upsert_policies(control, engine_id, render_policies(outputs))

    log.info("Done. Pin the engine in CDK to avoid drift:  make deploy POLICY_ENGINE_ARN=%s", engine_arn)


if __name__ == "__main__":
    main()
