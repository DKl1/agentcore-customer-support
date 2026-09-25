"""Simulated downstream dependencies (carrier tracking API and payment provider).

They are deliberately simple, but behave like real HTTP dependencies: calls have a
client-side timeout and can fail with 5xx. Fault injection is data-driven (a ``fault``
attribute on the seeded order) and only active when FAULT_INJECTION_ENABLED=true, which
the CDK stack sets for non-production stages only.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from .errors import DownstreamTimeout, DownstreamUnavailable

FAULT_CARRIER_TIMEOUT = "carrier_timeout"
FAULT_PAYMENT_HTTP_500 = "payment_http_500"


class CarrierClient:
    def __init__(self, *, timeout_seconds: float, faults_enabled: bool, sleep: Any = time.sleep) -> None:
        self._timeout = timeout_seconds
        self._faults_enabled = faults_enabled
        self._sleep = sleep

    def get_tracking(self, order: dict[str, Any]) -> dict[str, Any]:
        if self._faults_enabled and order.get("fault") == FAULT_CARRIER_TIMEOUT:
            # Simulates a hung carrier API: the client waits for its full timeout, then gives up.
            self._sleep(self._timeout)
            raise DownstreamTimeout(
                f"Carrier tracking API did not respond within {self._timeout:.1f}s",
                details={"dependency": "carrier-api", "timeout_seconds": self._timeout},
            )
        return dict(order.get("shipment") or {})


class PaymentProviderClient:
    def __init__(self, *, faults_enabled: bool) -> None:
        self._faults_enabled = faults_enabled

    def refund(self, *, order: dict[str, Any], amount_cents: int, idempotency_key: str) -> str:
        """Returns the provider's refund reference.

        A real provider (Stripe, Adyen, ...) also receives ``idempotency_key`` so that a
        retry after an ambiguous failure cannot double-charge at the provider either.
        """
        if self._faults_enabled and order.get("fault") == FAULT_PAYMENT_HTTP_500:
            raise DownstreamUnavailable(
                "Payment provider returned HTTP 500 Internal Server Error",
                http_status=500,
                details={"dependency": "payment-provider"},
            )
        del amount_cents, idempotency_key  # used by a real provider integration
        return f"pp_{uuid.uuid4().hex[:16]}"
