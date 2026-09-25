"""Exponential backoff with jitter for retryable tool failures."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 4.0

    def delay_for(self, attempt: int, rng: Callable[[], float] = random.random) -> float:
        """Delay before attempt ``attempt + 1`` ("equal jitter").

        The ceiling doubles each attempt (0.5s, 1s, 2s, ... capped at ``max_delay_seconds``);
        half of it is fixed and half is random. The fixed half keeps the backoff exponential,
        and the random half keeps concurrent sessions from retrying in lock-step.
        """
        if attempt < 1:
            raise ValueError("attempt is 1-based")
        ceiling = min(self.max_delay_seconds, self.base_delay_seconds * 2 ** (attempt - 1))
        return ceiling / 2 + rng() * ceiling / 2

    def should_retry(self, attempt: int) -> bool:
        return attempt < self.max_attempts
