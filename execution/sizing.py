"""Order quantity — pure, broker-agnostic, unit-testable.

Two regimes:
  - INDEX / option vehicle: ``lots × lot_size`` (the conviction 1-2 lot band the
    proposal already carries).
  - STOCK equity (cash, long-only): ``floor(max_amount / share_price)`` — the
    trader's per-trade rupee cap (default ₹10,000). Lot size is irrelevant for a
    cash equity buy.

Returns 0 on un-sizable inputs (bad price / non-positive cap); the caller turns a
0 into a clear rejection rather than placing a zero-qty order.
"""

from __future__ import annotations

import math

DEFAULT_MAX_AMOUNT = 10_000.0


def qty_for_order(
    *,
    segment: str,
    lots: int | None = None,
    lot_size: int | None = None,
    max_amount: float | None = None,
    share_price: float | None = None,
) -> int:
    """Shares (equity) or contracts (option) for one order. Never negative."""
    if segment == "equity":
        cap = DEFAULT_MAX_AMOUNT if max_amount is None else float(max_amount)
        if cap <= 0 or not share_price or share_price <= 0:
            return 0
        return max(0, int(math.floor(cap / share_price)))
    # option / index vehicle
    return max(0, int(lots or 0) * int(lot_size or 0))
