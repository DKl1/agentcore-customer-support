"""Classification of MCP tool results returned by the AgentCore Gateway.

Two kinds of failures reach the agent:

1. **Business/tool errors** produced by our Lambda. They arrive as a successful MCP result
   whose text is our JSON contract (``{"ok": false, "error": {..., "retryable": bool}}``).
   The Lambda decides retryability — the agent does not guess.
2. **Platform errors** produced by the Gateway or the transport (policy DENY, schema
   rejection, throttling, timeouts). They arrive as an MCP error result with free text, so
   they are classified by pattern. Policy denials are checked first and are never retried.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Outcome(StrEnum):
    SUCCESS = "success"
    RETRYABLE_ERROR = "retryable_error"
    FATAL_ERROR = "fatal_error"
    POLICY_DENIED = "policy_denied"


_POLICY_DENIED = re.compile(
    r"(policy|not\s+authori[sz]ed|unauthori[sz]ed|access\s*denied|forbidden|\bdenied\b)", re.IGNORECASE
)
_TRANSIENT = re.compile(
    r"(time[d\s-]*out|throttl|too\s+many\s+requests|\b429\b|\b50[234]\b|temporar|connection|unavailable)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ClassifiedResult:
    outcome: Outcome
    error_code: str | None = None
    http_status: int | None = None
    message: str | None = None

    @property
    def is_error(self) -> bool:
        return self.outcome is not Outcome.SUCCESS


def result_text(result: dict[str, Any]) -> str:
    return "\n".join(
        block["text"] for block in result.get("content", []) if isinstance(block, dict) and "text" in block
    )


def _parse_contract(text: str) -> dict[str, Any] | None:
    try:
        body = json.loads(text)
    except (TypeError, ValueError):
        return None
    return body if isinstance(body, dict) and "ok" in body else None


def classify(result: dict[str, Any]) -> ClassifiedResult:
    text = result_text(result)
    body = _parse_contract(text)

    if body is not None:
        if body.get("ok"):
            return ClassifiedResult(Outcome.SUCCESS)
        error = body.get("error") or {}
        return ClassifiedResult(
            Outcome.RETRYABLE_ERROR if error.get("retryable") else Outcome.FATAL_ERROR,
            error_code=error.get("code"),
            http_status=error.get("http_status"),
            message=error.get("message"),
        )

    if result.get("status") != "error":
        return ClassifiedResult(Outcome.SUCCESS)
    if _POLICY_DENIED.search(text):
        return ClassifiedResult(
            Outcome.POLICY_DENIED, error_code="POLICY_DENIED", http_status=403, message=text[:500]
        )
    if _TRANSIENT.search(text):
        return ClassifiedResult(Outcome.RETRYABLE_ERROR, error_code="GATEWAY_TRANSIENT", message=text[:500])
    return ClassifiedResult(Outcome.FATAL_ERROR, error_code="GATEWAY_ERROR", message=text[:500])
