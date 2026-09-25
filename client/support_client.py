"""Trusted-backend (BFF) client for the agent on AgentCore Runtime.

In production this code runs in the web/mobile backend after the end user has logged in.
It is responsible for what AgentCore does NOT do for you:

* deriving ``customer_id`` from the authenticated user (never from user text),
* mapping users to ``runtimeSessionId`` values (``<customer_id>-<uuid4>``, >= 33 chars),
* generating one ``operation_id`` per user action for end-to-end idempotency,
* retrying throttled invocations (botocore adaptive retry mode).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.config import Config

_RUNTIME_CLIENT_CONFIG = Config(
    retries={"mode": "adaptive", "max_attempts": 5},
    connect_timeout=10,
    read_timeout=300,
)


@dataclass(frozen=True, slots=True)
class AgentReply:
    response: str | None
    session_id: str | None
    trace_id: str | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> AgentReply:
        return cls(
            response=payload.get("response"),
            session_id=payload.get("session_id"),
            trace_id=payload.get("trace_id"),
            tool_calls=list(payload.get("tool_calls") or []),
            error=payload.get("error"),
            raw=payload,
        )

    def tools_used(self) -> list[str]:
        return [call["tool"] for call in self.tool_calls]


def _decode_body(body: bytes, content_type: str) -> dict[str, Any]:
    text = body.decode("utf-8")
    if "text/event-stream" in content_type:
        # Take the last SSE data frame (the final result).
        frames = [line[len("data:") :].strip() for line in text.splitlines() if line.startswith("data:")]
        text = frames[-1] if frames else "{}"
    data = json.loads(text)
    if isinstance(data, str):  # some SDK versions double-encode
        data = json.loads(data)
    return data


class SupportAgentClient:
    def __init__(self, runtime_arn: str, region: str, *, session: boto3.Session | None = None) -> None:
        self._runtime_arn = runtime_arn
        self._client = (session or boto3.Session()).client(
            "bedrock-agentcore", region_name=region, config=_RUNTIME_CLIENT_CONFIG
        )

    @staticmethod
    def new_session_id(customer_id: str) -> str:
        return f"{customer_id}-{uuid.uuid4().hex}"

    def invoke(
        self, *, customer_id: str, session_id: str, prompt: str, operation_id: str | None = None
    ) -> AgentReply:
        payload: dict[str, Any] = {"prompt": prompt, "customer_id": customer_id}
        if operation_id:
            payload["operation_id"] = operation_id
        response = self._client.invoke_agent_runtime(
            agentRuntimeArn=self._runtime_arn,
            runtimeSessionId=session_id,
            qualifier="DEFAULT",
            contentType="application/json",
            accept="application/json",
            payload=json.dumps(payload).encode("utf-8"),
        )
        body = response["response"].read()
        return AgentReply.from_payload(_decode_body(body, response.get("contentType", "application/json")))

    def stop_session(self, session_id: str) -> None:
        """Terminate the session's microVM (ends in-memory state; Memory persists)."""
        self._client.stop_runtime_session(
            agentRuntimeArn=self._runtime_arn, runtimeSessionId=session_id, qualifier="DEFAULT"
        )
