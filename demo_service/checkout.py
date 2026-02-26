"""Checkout module."""


def process_checkout(cart: dict) -> dict:
    """Process a checkout for the given *cart*."""
    total = sum(item["price"] * item["qty"] for item in cart.get("items", []))
    discount = total * 0.1 if cart.get("promo") else 0
    return {
        "status": "success",
        "total": round(total - discount, 2),
        "method": "new-checkout",
    }


def checkout_summary(order: dict) -> str:
    """Return a human-readable summary for an order."""
    return f"Order {order['id']}: ${order['total']:.2f} via new checkout"
