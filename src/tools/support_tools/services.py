"""Business logic for the three support tools."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from .downstream import CarrierClient, PaymentProviderClient
from .errors import NotFound, RefundExceedsBalance, RefundLimitExceeded
from .idempotency import IdempotencyStore, request_fingerprint
from .models import GetCustomerRequest, GetOrderStatusRequest, RefundRequest
from .repository import SupportRepository


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def _mask_phone(phone: str) -> str:
    return f"***{phone[-4:]}" if len(phone) >= 4 else "***"


class OrderService:
    def __init__(self, repository: SupportRepository, carrier: CarrierClient) -> None:
        self._repository = repository
        self._carrier = carrier

    def get_order_status(self, request: GetOrderStatusRequest) -> dict[str, Any]:
        order = self._repository.get_order(request.order_id)
        # Orders of other customers are reported as "not found" so ids cannot be enumerated.
        if not order or order["customer_id"] != request.customer_id:
            raise NotFound(f"Order {request.order_id} was not found for this customer")

        shipment = self._carrier.get_tracking(order)
        response: dict[str, Any] = {
            "order_id": order["order_id"],
            "status": order["status"],
            "placed_at": order.get("placed_at"),
            "items": order.get("items", []),
            "total_cents": order["total_cents"],
            "refunded_cents": order.get("refunded_cents", 0),
            "currency": order.get("currency", "USD"),
            "shipment": shipment,
            "delay_reason": order.get("delay_reason"),
            "estimated_delivery": order.get("estimated_delivery"),
        }
        if order.get("customer_note"):
            # Free text written by a human outside our control: label it as untrusted data so the
            # model treats it as content, never as instructions (indirect prompt-injection vector).
            response["customer_note"] = {"untrusted_text": order["customer_note"]}
        return response


class CustomerService:
    def __init__(self, repository: SupportRepository) -> None:
        self._repository = repository

    def get_customer(self, request: GetCustomerRequest) -> dict[str, Any]:
        customer = self._repository.get_customer(request.customer_id)
        if not customer:
            raise NotFound(f"Customer {request.customer_id} was not found")
        # Data minimisation: the agent never needs full contact details.
        return {
            "customer_id": customer["customer_id"],
            "name": customer["name"],
            "tier": customer.get("tier", "STANDARD"),
            "email": _mask_email(customer.get("email", "")),
            "phone": _mask_phone(customer.get("phone", "")),
            "member_since": customer.get("member_since"),
            "order_ids": sorted(customer.get("order_ids", [])),
        }


class RefundService:
    def __init__(
        self,
        repository: SupportRepository,
        idempotency: IdempotencyStore,
        payments: PaymentProviderClient,
        *,
        max_refund_cents: int,
    ) -> None:
        self._repository = repository
        self._idempotency = idempotency
        self._payments = payments
        self._max_refund_cents = max_refund_cents

    def refund(self, request: RefundRequest) -> dict[str, Any]:
        # Defence in depth: the Cedar policy at the Gateway denies this first. This check only
        # matters if the policy engine is detached, in LOG_ONLY mode, or misconfigured.
        if request.amount_cents > self._max_refund_cents:
            raise RefundLimitExceeded(
                f"Refunds above {self._max_refund_cents / 100:.2f} USD require human approval",
                details={"limit_cents": self._max_refund_cents},
            )

        pk = IdempotencyStore.scoped_key(request.customer_id, request.idempotency_key)
        # ``reason`` is excluded on purpose: a retry may paraphrase it, the money movement is identical.
        fingerprint = request_fingerprint(
            {
                "customer_id": request.customer_id,
                "order_id": request.order_id,
                "amount_cents": request.amount_cents,
            }
        )
        claim = self._idempotency.claim(pk, fingerprint)
        if claim.is_replay:
            return {**(claim.replayed_result or {}), "idempotent_replay": True}

        try:
            order, provider_reference = self._reserve_and_pay(request)
        except Exception:
            self._idempotency.release(pk)  # nothing was paid out — a retry may run again
            raise

        refund_id = f"rf_{uuid.uuid4().hex[:20]}"
        created_at = datetime.now(UTC).isoformat()
        result = {
            "refund_id": refund_id,
            "status": "SUCCEEDED",
            "order_id": request.order_id,
            "amount_cents": request.amount_cents,
            "currency": order.get("currency", "USD"),
            "provider_reference": provider_reference,
            "created_at": created_at,
        }
        # From here on money has moved: never release the lock. If the commit fails, the lock expires
        # and a retry re-runs with the same key, which the provider de-duplicates.
        self._repository.commit_refund(
            refund={
                **result,
                "customer_id": request.customer_id,
                "reason": request.reason,
                "idempotency_key": request.idempotency_key,
            },
            idempotency_pk=pk,
            result=result,
        )
        return {**result, "idempotent_replay": False}

    def _reserve_and_pay(self, request: RefundRequest) -> tuple[dict[str, Any], str]:
        order = self._repository.get_order(request.order_id)
        if not order or order["customer_id"] != request.customer_id:
            raise NotFound(f"Order {request.order_id} was not found for this customer")

        remaining = order["total_cents"] - order.get("refunded_cents", 0)
        if request.amount_cents > remaining:
            raise RefundExceedsBalance(
                "Refund amount exceeds the refundable balance of the order",
                details={"refundable_cents": remaining},
            )
        reserved = self._repository.reserve_refund(
            order_id=request.order_id,
            customer_id=request.customer_id,
            amount_cents=request.amount_cents,
            total_cents=order["total_cents"],
        )
        if not reserved:
            raise RefundExceedsBalance("Refundable balance changed concurrently; nothing was refunded")

        try:
            provider_reference = self._payments.refund(
                order=order,
                amount_cents=request.amount_cents,
                idempotency_key=IdempotencyStore.scoped_key(request.customer_id, request.idempotency_key),
            )
        except Exception:
            self._repository.release_refund_reservation(
                order_id=request.order_id, amount_cents=request.amount_cents
            )
            raise
        return order, provider_reference
