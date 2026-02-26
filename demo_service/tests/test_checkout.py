"""Tests for the checkout module."""

from demo_service.checkout import checkout_summary, process_checkout


def test_process_checkout_with_promo():
    cart = {
        "items": [
            {"name": "Widget", "price": 10.00, "qty": 2},
            {"name": "Gadget", "price": 25.00, "qty": 1},
        ],
        "promo": True,
    }
    result = process_checkout(cart)
    assert result["status"] == "success"
    assert result["method"] == "new-checkout"
    # (10*2 + 25*1) = 45, minus 10% promo = 40.50
    assert result["total"] == 40.5


def test_process_checkout_without_promo():
    cart = {
        "items": [
            {"name": "Widget", "price": 10.00, "qty": 3},
        ],
    }
    result = process_checkout(cart)
    assert result["status"] == "success"
    assert result["total"] == 30.0


def test_checkout_summary():
    order = {"id": "ORD-123", "total": 45.50}
    summary = checkout_summary(order)
    assert "ORD-123" in summary
    assert "$45.50" in summary
