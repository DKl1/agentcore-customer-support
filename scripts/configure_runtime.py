"""Harden the AgentCore Runtime settings that CloudFormation does not expose yet.

* ``metadataConfiguration.requireMMDSV2 = true`` — session-token-protected metadata service
  (mandatory for invocation since 2026-06-30).
* Retention of the Runtime log group, which the service creates itself (so CloudFormation
  cannot own it); without this, logs are kept forever.

UpdateAgentRuntime has full-replace semantics, so the current configuration is read back
and re-sent unchanged apart from the hardened fields. Re-run after every ``cdk deploy``
(``make deploy`` does this); a CloudFormation update of the runtime may reset the flag.

Usage: python scripts/configure_runtime.py [--stage dev]
"""

from __future__ import annotations

import argparse
from typing import Any

from _common import client, load_outputs, log, wait_until

LOG_RETENTION_DAYS = 30
_UPDATABLE_FIELDS = (
    "agentRuntimeArtifact",
    "roleArn",
    "networkConfiguration",
    "description",
    "authorizerConfiguration",
    "requestHeaderConfiguration",
    "protocolConfiguration",
    "lifecycleConfiguration",
    "environmentVariables",
    "filesystemConfigurations",
    "capacityProviderConfiguration",
)


def require_mmdsv2(control: Any, runtime_id: str) -> None:
    runtime = control.get_agent_runtime(agentRuntimeId=runtime_id)
    if (runtime.get("metadataConfiguration") or {}).get("requireMMDSV2"):
        log.info("Runtime %s already requires MMDSv2", runtime_id)
        return

    fields = {field: runtime[field] for field in _UPDATABLE_FIELDS if runtime.get(field) is not None}
    control.update_agent_runtime(
        agentRuntimeId=runtime_id, metadataConfiguration={"requireMMDSV2": True}, **fields
    )
    wait_until(
        lambda: control.get_agent_runtime(agentRuntimeId=runtime_id)["status"],
        ready={"READY"},
        failed={"UPDATE_FAILED", "CREATE_FAILED"},
        what="runtime update",
    )
    log.info("Runtime %s now requires MMDSv2", runtime_id)


def set_log_retention(logs: Any, log_group_name: str) -> None:
    logs.put_retention_policy(logGroupName=log_group_name, retentionInDays=LOG_RETENTION_DAYS)
    log.info("Log group %s retention set to %d days", log_group_name, LOG_RETENTION_DAYS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default="dev")
    args = parser.parse_args()

    outputs = load_outputs(args.stage)
    require_mmdsv2(client("bedrock-agentcore-control", outputs), outputs["RuntimeId"])
    set_log_retention(client("logs", outputs), outputs["RuntimeLogGroup"])


if __name__ == "__main__":
    main()
