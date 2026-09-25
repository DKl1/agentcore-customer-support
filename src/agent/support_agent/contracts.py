"""Invocation contract between the trusted backend (BFF) and the agent.

AgentCore Runtime (non-harness) performs no validation of the payload, so this is the
agent's input-validation boundary:

* the payload must match the schema exactly (``extra="forbid"`` rejects smuggled
  ``messages`` / ``toolUse`` blocks),
* ``customer_id`` is supplied by the authenticated backend, never by the end user's text,
* the runtime session id must be bound to that customer (defence against a backend bug
  routing user A's request into user B's session).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator

CUSTOMER_ID_PATTERN = r"^CUST-\d{4,10}$"
OPERATION_ID_PATTERN = r"^[A-Za-z0-9_.:-]{8,128}$"
MIN_SESSION_ID_LENGTH = 33
MAX_PROMPT_CHARS = 4000

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class InvalidRequest(ValueError):
    """The invocation payload or session binding is invalid; nothing was executed."""


class InvocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    customer_id: str = Field(pattern=CUSTOMER_ID_PATTERN)
    operation_id: str | None = Field(default=None, pattern=OPERATION_ID_PATTERN)

    @field_validator("prompt")
    @classmethod
    def _strip_control_characters(cls, value: str) -> str:
        cleaned = _CONTROL_CHARS.sub("", value).strip()
        if not cleaned:
            raise ValueError("prompt is empty after sanitisation")
        return cleaned


@dataclass(frozen=True, slots=True)
class InvocationContext:
    customer_id: str
    session_id: str
    operation_id: str | None = None

    @classmethod
    def create(
        cls, request: InvocationRequest, session_id: str | None, *, enforce_session_binding: bool = True
    ) -> InvocationContext:
        if not session_id or len(session_id) < MIN_SESSION_ID_LENGTH:
            raise InvalidRequest(f"runtimeSessionId must be at least {MIN_SESSION_ID_LENGTH} characters")
        if enforce_session_binding and not session_id.startswith(f"{request.customer_id}-"):
            raise InvalidRequest("runtimeSessionId is not bound to the authenticated customer")
        return cls(customer_id=request.customer_id, session_id=session_id, operation_id=request.operation_id)

    def refund_idempotency_key(self, order_id: str, amount_cents: int) -> str:
        """Platform-owned idempotency key — the LLM never chooses it.

        * If the backend sent an ``operation_id`` (one per user action, like an
          ``Idempotency-Key`` HTTP header), it is used as-is, e.g. ``operation-123``.
        * Otherwise it is derived deterministically from session + order + amount, so the same
          refund requested again in the same conversation is de-duplicated as well.
        """
        if self.operation_id:
            return self.operation_id
        digest = hashlib.sha256(f"{self.session_id}|{order_id}|{amount_cents}".encode()).hexdigest()
        return f"auto-{digest[:40]}"
