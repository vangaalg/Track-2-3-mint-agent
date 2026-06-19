"""Build and (optionally) place a Breeze order from an approved proposal.

Two safety layers before anything reaches the broker:
  1. ``place`` refuses a proposal that isn't ENTER.
  2. ``place`` is dry-run unless ``live=True`` AND ``EXECUTION_LIVE=1`` in the env
     AND a broker ``place_fn`` is supplied. Absent any of these it returns the
     would-be order and places nothing.
"""

from __future__ import annotations

import os
import re
from typing import Callable

from analysis.proposal import TradeProposal

LOT_SIZE = 75
_VEHICLE_RE = re.compile(r"^(?P<sym>\S+)\s+(?P<strike>\d+)\s+(?P<right>CE|PE)")


def build_order(proposal: TradeProposal) -> dict:
    """Translate a proposal into Breeze order params (does not place anything)."""
    if proposal.direction not in ("long", "short"):
        raise ValueError("cannot build an order for a non-directional proposal")
    m = _VEHICLE_RE.match(proposal.vehicle or "")
    if not m:
        raise ValueError(f"cannot parse vehicle {proposal.vehicle!r}")

    qty = int((proposal.size_lots or 0) * LOT_SIZE)
    return {
        "stock_code": m["sym"],
        "exchange_code": "NFO",
        "product": "options",
        "action": "buy",                 # Trade 1 buys the deep-ITM option
        "order_type": "limit",
        "quantity": qty,
        "price": proposal.entry,
        "strike_price": int(m["strike"]),
        "right": "call" if m["right"] == "CE" else "put",
        "validity": "day",
        "stoploss": proposal.stop,
    }


def place(
    proposal: TradeProposal, live: bool = False, place_fn: Callable | None = None
) -> dict:
    """Place (or dry-run) an APPROVED proposal. Never auto-fires.

    Returns ``{"status": "rejected"|"dry_run"|"placed", "order": ...}``. Live
    placement requires ``live=True`` + ``EXECUTION_LIVE=1`` + a ``place_fn``; any
    missing piece falls back to dry-run.
    """
    if not proposal.is_enter:
        return {"status": "rejected", "reason": "proposal is STAND_DOWN", "order": None}

    order = build_order(proposal)
    gated = live and os.environ.get("EXECUTION_LIVE") == "1" and place_fn is not None
    if not gated:
        return {"status": "dry_run", "order": order}

    result = place_fn(**order)
    return {"status": "placed", "order": order, "broker_response": result}
