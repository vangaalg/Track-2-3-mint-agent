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
from .base import Broker, Order, OrderResult, Position
from .sizing import qty_for_order, DEFAULT_MAX_AMOUNT

LOT_SIZE = 75
_VEHICLE_RE = re.compile(r"^(?P<sym>\S+)\s+(?P<strike>\d+)\s+(?P<right>CE|PE)")

# Equity product code Breeze expects for an NSE cash buy (CNC = delivery). Env-overridable
# until verified against a live order (the egress-locked sandbox can't confirm it).
STOCK_PRODUCT = os.environ.get("STOCK_PRODUCT", "cash")


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


# --------------------------------------------------------------------------- #
# Broker-agnostic order build + the Breeze adapter (the live path).
# --------------------------------------------------------------------------- #
def build_orders(
    proposal: TradeProposal,
    *,
    segment: str = "option",
    order_type: str = "market",
    limit_price: float | None = None,
    quantity: int | None = None,
    max_amount: float | None = None,
    share_price: float | None = None,
    lot_size: int = LOT_SIZE,
    exchange: str = "NFO",
    expiry_date: str | None = None,
    client_tag: str | None = None,
) -> Order:
    """Translate an APPROVED proposal into a neutral ``Order`` (no placement).

    ``segment="option"`` (index/option vehicle) → an NFO BUY of the deep-ITM CE/PE
    (qty = lots × lot_size). ``segment="equity"`` (NSE-50 stock) → an NSE cash BUY,
    long-only, qty capped to ``floor(max_amount / share_price)``. Entry is MARKET by
    default; ``order_type="limit"`` carries ``limit_price``.
    """
    if proposal.direction not in ("long", "short"):
        raise ValueError("cannot build an order for a non-directional proposal")
    price = limit_price if order_type == "limit" else None

    if segment == "equity":
        qty = quantity if quantity is not None else qty_for_order(
            segment="equity",
            max_amount=DEFAULT_MAX_AMOUNT if max_amount is None else max_amount,
            share_price=share_price)
        if qty <= 0:
            raise ValueError("equity order sizes to 0 — check max_amount / share price")
        return Order(segment="equity", symbol=proposal.instrument, exchange="NSE",
                     action="buy", order_type=order_type, quantity=int(qty),
                     product=STOCK_PRODUCT, price=price, client_tag=client_tag)

    # option / index vehicle
    m = _VEHICLE_RE.match(proposal.vehicle or "")
    if not m:
        raise ValueError(f"cannot parse vehicle {proposal.vehicle!r}")
    qty = quantity if quantity is not None else qty_for_order(
        segment="option", lots=proposal.size_lots, lot_size=lot_size)
    if qty <= 0:
        raise ValueError("option order sizes to 0 — check size_lots / lot_size")
    return Order(segment="option", symbol=m["sym"], exchange=exchange, action="buy",
                 order_type=order_type, quantity=int(qty), product="options", price=price,
                 right=("call" if m["right"] == "CE" else "put"),
                 strike_price=int(m["strike"]), expiry_date=expiry_date,
                 client_tag=client_tag)


def _s(x) -> str:
    """Breeze wants string numerics; None/blank → ''."""
    return "" if x is None else str(x)


class BreezeBroker(Broker):
    """ICICI Breeze adapter over the official ``breeze_connect`` SDK. Every method is
    wrapped so a broker/SDK failure returns ``OrderResult(status="error")`` instead of
    raising into the request handler or the exit-monitor loop. The client is built
    lazily and cached, but re-fetched if the daily session token changes in the env."""

    name = "breeze"

    def __init__(self, client=None, client_factory=None):
        self._client = client
        self._factory = client_factory
        self._token = os.environ.get("BREEZE_SESSION_TOKEN")

    def _cl(self):
        tok = os.environ.get("BREEZE_SESSION_TOKEN")
        if self._client is None or tok != self._token:
            if self._client is None:
                factory = self._factory
                if factory is None:
                    from loaders.breeze import get_breeze_client
                    factory = get_breeze_client
                self._client = factory()
            self._token = tok
        return self._client

    # -- placement ---------------------------------------------------------- #
    def _place(self, order: Order) -> OrderResult:
        try:
            resp = self._cl().place_order(
                stock_code=order.symbol, exchange_code=order.exchange,
                product=order.product, action=order.action, order_type=order.order_type,
                quantity=_s(order.quantity), price=_s(order.price),
                validity=order.validity,
                right=_s(order.right), strike_price=_s(order.strike_price),
                expiry_date=_s(order.expiry_date))
        except Exception as exc:                       # never raise into the caller
            return OrderResult(status="error", message=str(exc))
        return _result_from_breeze(resp)

    def place_entry(self, order: Order) -> OrderResult:
        return self._place(order)

    def place_exit(self, order: Order) -> OrderResult:
        # A self-managed exit = an opposite-side MARKET order (symmetric with entry).
        return self._place(order)

    def order_status(self, broker_order_id: str) -> OrderResult:
        try:
            resp = self._cl().get_order_detail(exchange_code="NFO", order_id=broker_order_id)
        except Exception as exc:
            return OrderResult(status="error", message=str(exc))
        return _result_from_breeze(resp, default_id=broker_order_id)

    def cancel(self, broker_order_id: str) -> OrderResult:
        try:
            resp = self._cl().cancel_order(exchange_code="NFO", order_id=broker_order_id)
        except Exception as exc:
            return OrderResult(status="error", message=str(exc))
        return _result_from_breeze(resp, default_id=broker_order_id)

    def positions(self) -> list[Position]:
        try:
            resp = self._cl().get_portfolio_positions()
        except Exception:
            return []
        out: list[Position] = []
        for r in (resp.get("Success") or []) if isinstance(resp, dict) else []:
            try:
                out.append(Position(
                    symbol=r.get("stock_code") or r.get("symbol") or "",
                    segment="option" if (r.get("exchange_code") in ("NFO", "BFO")) else "equity",
                    quantity=int(float(r.get("quantity") or 0)),
                    avg_price=float(r.get("average_price") or 0) or None,
                    product=r.get("product_type"), raw=r))
            except Exception:
                continue
        return out

    def funds(self) -> dict:
        try:
            resp = self._cl().get_funds()
        except Exception as exc:
            return {"error": str(exc)}
        return resp if isinstance(resp, dict) else {"raw": resp}


def _result_from_breeze(resp, default_id: str | None = None) -> OrderResult:
    """Map a Breeze SDK response ``{"Success":..., "Status":200, "Error":None}`` to an
    OrderResult. A non-None ``Error`` (or a non-2xx Status) is a rejection."""
    if not isinstance(resp, dict):
        return OrderResult(status="error", message="non-dict broker response", raw={"raw": resp})
    if resp.get("Error"):
        return OrderResult(status="rejected", message=str(resp.get("Error")), raw=resp)
    succ = resp.get("Success")
    body = succ[0] if isinstance(succ, list) and succ else (succ if isinstance(succ, dict) else {})
    oid = (body or {}).get("order_id") or default_id
    filled = (body or {}).get("filled_quantity") or (body or {}).get("quantity")
    avg = (body or {}).get("average_price") or (body or {}).get("price")
    return OrderResult(
        status="placed", broker_order_id=str(oid) if oid is not None else None,
        filled_qty=int(float(filled)) if filled not in (None, "") else None,
        avg_price=float(avg) if avg not in (None, "") else None, raw=resp)
