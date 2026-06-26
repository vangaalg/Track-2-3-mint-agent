"""Broker-agnostic order contract + the ``Broker`` interface.

The cockpit speaks in these neutral dataclasses; each broker adapter
(``BreezeBroker`` now, ``KiteBroker`` later) maps them to its own SDK. ``Order``
carries the contract fields STRUCTURED (strike / right / expiry separate) rather
than a pre-formatted symbol, because Breeze wants stock_code + strike + right +
expiry while Kite wants a single tradingsymbol — each adapter formats its own.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Order:
    """One order to send to a broker. ``quantity`` is in SHARES/CONTRACTS already
    (lots already multiplied out), never in lots."""

    segment: str                          # "equity" | "option"
    symbol: str                           # stock_code, e.g. "NIFTY" / "RELIANCE"
    exchange: str                         # "NSE" | "NFO" | "BFO"
    action: str                           # "buy" | "sell"
    order_type: str                       # "market" | "limit"
    quantity: int
    product: str                          # "cash"/"CNC" (equity) | "options" (NFO)
    price: float | None = None            # required for a limit order
    validity: str = "day"
    # Option-only (blank for equity)
    right: str | None = None              # "call" | "put"
    strike_price: int | None = None
    expiry_date: str | None = None
    # Idempotency / audit tag — the caller sets f"{strategy}:{ts}".
    client_tag: str | None = None


@dataclass
class OrderResult:
    """The outcome of a placement (or a dry-run / error). Never raised — returned."""

    status: str                           # "placed" | "rejected" | "dry_run" | "error"
    broker_order_id: str | None = None
    raw: dict = field(default_factory=dict)
    message: str | None = None
    filled_qty: int | None = None
    avg_price: float | None = None


@dataclass
class Position:
    """A broker-reported open position (for boot reconciliation)."""

    symbol: str
    segment: str
    quantity: int                         # signed
    avg_price: float | None = None
    product: str | None = None
    raw: dict = field(default_factory=dict)


class Broker(ABC):
    """The interface every adapter implements. Methods NEVER raise into the caller —
    failures come back as ``OrderResult(status="error", ...)`` so a request handler
    or the exit-monitor loop can degrade gracefully."""

    name: str = "broker"

    @abstractmethod
    def place_entry(self, order: Order) -> OrderResult: ...

    @abstractmethod
    def place_exit(self, order: Order) -> OrderResult:
        """Place a market exit (the caller has already set ``action`` to the
        opposite side and ``order_type='market'``)."""

    @abstractmethod
    def order_status(self, broker_order_id: str) -> OrderResult: ...

    @abstractmethod
    def cancel(self, broker_order_id: str) -> OrderResult: ...

    @abstractmethod
    def positions(self) -> list[Position]: ...

    @abstractmethod
    def funds(self) -> dict: ...
