"""Pricing engine — uses the 'use-v2-pricing-engine' feature flag."""

from demo_service.feature_flags import is_enabled


def calculate_price(base_price: float, quantity: int, tier: str = "standard") -> float:
    """Calculate the final price for a line item.

    The v2 pricing engine flag has been *off* for 100 days — the v2 branch
    is dead code that was never fully rolled out.
    """
    if is_enabled("use-v2-pricing-engine"):
        # V2 pricing — dead code (flag always off)
        multipliers = {"basic": 1.0, "standard": 0.9, "premium": 0.8}
        multiplier = multipliers.get(tier, 1.0)
        volume_discount = 0.05 if quantity > 100 else 0
        return round(base_price * quantity * multiplier * (1 - volume_discount), 2)
    else:
        # V1 pricing — always taken
        if tier == "premium":
            return round(base_price * quantity * 0.85, 2)
        return round(base_price * quantity, 2)


def get_pricing_tier_info(tier: str) -> dict:
    """Return metadata about a pricing tier."""
    if is_enabled("use-v2-pricing-engine"):
        # V2 tier info — dead code
        return {
            "tier": tier,
            "engine": "v2",
            "features": ["volume-discount", "tier-multiplier", "dynamic-pricing"],
        }
    else:
        return {
            "tier": tier,
            "engine": "v1",
            "features": ["flat-rate", "premium-discount"],
        }
