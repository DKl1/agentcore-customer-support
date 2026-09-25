"""Deterministic guards that run before every tool call (outside the model's control)."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class GuardViolation:
    code: str
    message: str


class ToolCallBudget:
    """Stops LLM loops within one agent turn.

    * ``max_identical_calls`` — the same tool with the same arguments may run at most N times
      (repeated identical calls are the loop signature seen in traces).
    * ``max_calls`` — hard ceiling on the number of tool calls per turn.

    Internal retries of one call (backoff) do not count; only calls requested by the model do.
    """

    def __init__(self, *, max_calls: int, max_identical_calls: int) -> None:
        self._max_calls = max_calls
        self._max_identical = max_identical_calls
        self._total = 0
        self._by_signature: Counter[str] = Counter()

    @property
    def total_calls(self) -> int:
        return self._total

    def check(self, tool_name: str, arguments: dict[str, Any] | None) -> GuardViolation | None:
        signature = f"{tool_name}:{json.dumps(arguments or {}, sort_keys=True, default=str)}"
        self._total += 1
        self._by_signature[signature] += 1
        if self._total > self._max_calls:
            return GuardViolation(
                "TOOL_BUDGET_EXCEEDED",
                f"Tool-call budget of {self._max_calls} calls per turn exhausted. Stop calling tools and "
                "answer the customer with the information you already have.",
            )
        if self._by_signature[signature] > self._max_identical:
            return GuardViolation(
                "REPEATED_TOOL_CALL",
                f"{tool_name} was already called {self._max_identical} times with identical arguments and the "
                "result will not change. Do not call it again; answer the customer now.",
            )
        return None


@dataclass(slots=True)
class ToolCallRecord:
    tool: str
    outcome: str
    attempts: int
    latency_ms: int
    error_code: str | None = None


@dataclass(slots=True)
class ToolCallLedger:
    """Per-invocation audit of tool calls, returned to the caller and logged for evaluation."""

    records: list[ToolCallRecord] = field(default_factory=list)

    def add(self, record: ToolCallRecord) -> None:
        self.records.append(record)

    def as_dicts(self) -> list[dict[str, Any]]:
        return [
            {
                "tool": r.tool,
                "outcome": r.outcome,
                "attempts": r.attempts,
                "latency_ms": r.latency_ms,
                "error_code": r.error_code,
            }
            for r in self.records
        ]
