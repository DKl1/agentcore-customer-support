"""Runtime configuration, read once per cold start from environment variables set by CDK."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True, slots=True)
class Settings:
    customers_table: str
    orders_table: str
    refunds_table: str
    idempotency_table: str
    max_refund_cents: int = 100_000
    fault_injection_enabled: bool = False
    downstream_timeout_seconds: float = 2.5
    idempotency_lock_seconds: int = 60
    idempotency_ttl_days: int = 7

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            customers_table=os.environ["CUSTOMERS_TABLE"],
            orders_table=os.environ["ORDERS_TABLE"],
            refunds_table=os.environ["REFUNDS_TABLE"],
            idempotency_table=os.environ["IDEMPOTENCY_TABLE"],
            max_refund_cents=int(os.environ.get("MAX_REFUND_CENTS", "100000")),
            fault_injection_enabled=_env_bool("FAULT_INJECTION_ENABLED"),
            downstream_timeout_seconds=float(os.environ.get("DOWNSTREAM_TIMEOUT_SECONDS", "2.5")),
            idempotency_lock_seconds=int(os.environ.get("IDEMPOTENCY_LOCK_SECONDS", "60")),
            idempotency_ttl_days=int(os.environ.get("IDEMPOTENCY_TTL_DAYS", "7")),
        )
