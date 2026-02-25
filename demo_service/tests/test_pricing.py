"""Tests for the pricing module."""

from demo_service.pricing import calculate_price, get_pricing_tier_info


def test_calculate_price_standard():
    price = calculate_price(10.0, 5, "standard")
    assert price == 50.0


def test_calculate_price_premium():
    price = calculate_price(100.0, 2, "premium")
    assert price == 170.0  # 100 * 2 * 0.85


def test_get_pricing_tier_info():
    info = get_pricing_tier_info("standard")
    assert info["engine"] == "v1"
    assert "flat-rate" in info["features"]
