"""Typed tool errors.

Every error that leaves the Lambda is converted into a structured payload::

    {"ok": false, "error": {"code": "...", "message": "...", "http_status": 504, "retryable": true}}

The ``retryable`` flag is the contract with the agent-side retry policy: only errors
flagged as retryable are retried with exponential backoff. Business-rule violations
(validation, limits, ownership) are never retried.
"""

from __future__ import annotations

from typing import Any, ClassVar


class ToolError(Exception):
    code: ClassVar[str] = "TOOL_ERROR"
    http_status: ClassVar[int] = 500
    retryable: ClassVar[bool] = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "http_status": self.http_status,
            "retryable": self.retryable,
        }
        if self.details:
            payload["details"] = self.details
        return payload


class InvalidParameters(ToolError):
    code = "INVALID_PARAMETERS"
    http_status = 400


class UnknownTool(ToolError):
    code = "UNKNOWN_TOOL"
    http_status = 400


class NotFound(ToolError):
    """Also used when a resource exists but belongs to another customer (prevents enumeration)."""

    code = "NOT_FOUND"
    http_status = 404


class RefundLimitExceeded(ToolError):
    """Backend safety net. The primary control is the Cedar policy at the Gateway."""

    code = "REFUND_LIMIT_EXCEEDED"
    http_status = 403


class RefundExceedsBalance(ToolError):
    code = "REFUND_EXCEEDS_BALANCE"
    http_status = 409


class IdempotencyKeyReused(ToolError):
    """Same idempotency key sent with a different payload — a client bug, never retry."""

    code = "IDEMPOTENCY_KEY_REUSED"
    http_status = 422


class OperationInProgress(ToolError):
    """Another attempt with the same idempotency key is still running."""

    code = "OPERATION_IN_PROGRESS"
    http_status = 409
    retryable = True


class DownstreamTimeout(ToolError):
    code = "DOWNSTREAM_TIMEOUT"
    http_status = 504
    retryable = True


class DownstreamUnavailable(ToolError):
    code = "DOWNSTREAM_UNAVAILABLE"
    retryable = True

    def __init__(
        self, message: str, *, http_status: int = 503, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message, details=details)
        self.http_status = http_status  # type: ignore[misc]  # instance override of the class default


class InternalError(ToolError):
    code = "INTERNAL_ERROR"
    http_status = 500
