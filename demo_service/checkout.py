"""Checkout module — uses the 'enable-new-checkout-flow' feature flag."""

from demo_service.feature_flags import is_enabled


def process_checkout(cart: dict) -> dict:
    """Process a checkout for the given *cart*.

    The old checkout path is dead code — the flag has been on for 120+ days.
    """
    if is_enabled("enable-new-checkout-flow"):
        # New checkout flow (always taken)
        total = sum(item["price"] * item["qty"] for item in cart.get("items", []))
        discount = total * 0.1 if cart.get("promo") else 0
        return {
            "status": "success",
            "total": round(total - discount, 2),
            "method": "new-checkout",
        }
    else:
        # Legacy checkout — dead code path
        total = sum(item["price"] for item in cart.get("items", []))
        return {
            "status": "success",
            "total": total,
            "method": "legacy-checkout",
        }


def checkout_summary(order: dict) -> str:
    """Return a human-readable summary for an order."""
    if is_enabled("enable-new-checkout-flow"):
        return f"Order {order['id']}: ${order['total']:.2f} via new checkout"
    else:
        return f"Order {order['id']}: ${order['total']:.2f} via legacy checkout"
