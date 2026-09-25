from __future__ import annotations

import pytest
from support_tools.errors import DownstreamTimeout, NotFound
from support_tools.models import GetCustomerRequest, GetOrderStatusRequest
from support_tools.services import CustomerService, OrderService


def test_delayed_order_explains_reason(orders: OrderService) -> None:
    result = orders.get_order_status(GetOrderStatusRequest(customer_id="CUST-1001", order_id="123"))

    assert result["status"] == "DELAYED"
    assert "backlog" in result["delay_reason"]
    assert result["shipment"]["tracking_status"] == "HELD_AT_HUB"


def test_other_customers_order_is_reported_as_not_found(orders: OrderService) -> None:
    with pytest.raises(NotFound):
        orders.get_order_status(GetOrderStatusRequest(customer_id="CUST-1001", order_id="321"))


def test_carrier_timeout_is_retryable(orders: OrderService) -> None:
    with pytest.raises(DownstreamTimeout) as error:
        orders.get_order_status(GetOrderStatusRequest(customer_id="CUST-1001", order_id="777"))
    assert error.value.retryable is True
    assert error.value.http_status == 504


def test_customer_note_is_marked_untrusted(orders: OrderService) -> None:
    result = orders.get_order_status(GetOrderStatusRequest(customer_id="CUST-1001", order_id="456"))
    assert "untrusted_text" in result["customer_note"]


def test_customer_profile_is_masked(customers: CustomerService) -> None:
    profile = customers.get_customer(GetCustomerRequest(customer_id="CUST-1001"))

    assert profile["name"] == "Jane Doe"
    assert profile["email"] == "j***@example.com"
    assert profile["phone"].endswith("1234") and profile["phone"].startswith("***")
    assert "123" in profile["order_ids"]
