"""Mock retail backend: deterministic data so evaluation results are reproducible (演示数据 / demo data)."""

from strands import tool

ORDERS = {
    "ORD-1001": {"item": "Bluetooth headphones", "category": "electronics", "days_since_delivery": 12,
                 "status": "delivered", "price": 59.0},
    "ORD-1002": {"item": "Wool sweater", "category": "apparel", "days_since_delivery": 45,
                 "status": "delivered", "price": 80.0},
    "ORD-1003": {"item": "Coffee grinder", "category": "home", "days_since_delivery": None,
                 "status": "in_transit", "price": 35.0},
}
POLICY = {
    "electronics": "Electronics can be returned within 30 days of delivery if undamaged. Refund in 3-5 business days.",
    "apparel": "Apparel can be returned within 30 days of delivery if unwashed and unworn.",
    "home": "Home goods can be returned within 14 days of delivery.",
}
_RMA_SEQ = {"n": 5000}


@tool
def get_order(order_id: str) -> dict:
    """Look up an order by its id (format ORD-1234). Returns item, category, status and days since delivery."""
    order = ORDERS.get(order_id.strip().upper())
    return {"order_id": order_id, **order} if order else {"error": f"order {order_id} not found"}


@tool
def get_return_policy(category: str) -> str:
    """Return the return policy text for a product category (electronics, apparel, home)."""
    return POLICY.get(category.strip().lower(), "No return policy found for this category.")


@tool
def create_return(order_id: str, reason: str) -> dict:
    """Create a return (RMA) for an order that is eligible under the return policy. Returns the RMA number."""
    order = ORDERS.get(order_id.strip().upper())
    if not order:
        return {"error": f"order {order_id} not found"}
    if order["status"] != "delivered":
        return {"error": "order not delivered yet; cannot create a return"}
    _RMA_SEQ["n"] += 1
    return {"rma": f"RMA-{_RMA_SEQ['n']}", "order_id": order_id, "reason": reason}


TOOLS = [get_order, get_return_policy, create_return]
