"""Zerodha (Kite Connect) adapter — interface stub, filled in when we add Kite.

Kite differs from Breeze in one important way: it identifies a contract by a single
``tradingsymbol`` (e.g. ``NIFTY24JUN23600CE`` / ``RELIANCE``) plus ``exchange``,
rather than Breeze's stock_code + strike_price + right + expiry_date. Because our
``Order`` keeps those fields STRUCTURED, this adapter formats the tradingsymbol
itself — the cockpit code above never changes when we switch brokers.

Mapping to fill in later (``kiteconnect.KiteConnect``):
  place_entry  -> kite.place_order(variety="regular", exchange, tradingsymbol,
                  transaction_type="BUY", quantity, product="CNC"/"MIS"/"NRML",
                  order_type="MARKET"/"LIMIT", price, validity="DAY")
  place_exit   -> same with transaction_type="SELL", order_type="MARKET"
  order_status -> kite.order_history(order_id)[-1]
  cancel       -> kite.cancel_order(variety, order_id)
  positions    -> kite.positions()["net"]
  funds        -> kite.margins()
"""

from __future__ import annotations

from .base import Broker, Order, OrderResult, Position

_PENDING = "Kite (Zerodha) adapter not implemented yet — use BreezeBroker."


class KiteBroker(Broker):
    name = "kite"

    def __init__(self, client=None):
        self._client = client

    def place_entry(self, order: Order) -> OrderResult:
        raise NotImplementedError(_PENDING)

    def place_exit(self, order: Order) -> OrderResult:
        raise NotImplementedError(_PENDING)

    def order_status(self, broker_order_id: str) -> OrderResult:
        raise NotImplementedError(_PENDING)

    def cancel(self, broker_order_id: str) -> OrderResult:
        raise NotImplementedError(_PENDING)

    def positions(self) -> list[Position]:
        raise NotImplementedError(_PENDING)

    def funds(self) -> dict:
        raise NotImplementedError(_PENDING)
