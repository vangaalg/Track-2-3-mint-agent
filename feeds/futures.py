"""NIFTY futures quote — the missing leg of the 3:15 CAS Excess-Premium read.

The CAS setup needs ``EP = Futures − Spot − FairCarry`` at 15:13 IST, and the system
only pulled spot + option chains. This fetches the near-month NIFTY futures LTP from
Breeze (``get_quotes`` on NFO, monthly last-Tuesday expiry). Pure parse + injectable
client → offline-testable; the live pull runs on the deployed service.
"""

from __future__ import annotations

from loaders.breeze import get_breeze_client
from feeds.breeze_oi import _expiry_iso


def parse_futures_ltp(resp) -> float | None:
    """The LTP out of a Breeze get_quotes response (None on any shape surprise)."""
    try:
        if not isinstance(resp, dict) or resp.get("Error"):
            return None
        rows = resp.get("Success") or []
        if not rows:
            return None
        ltp = rows[0].get("ltp")
        return float(ltp) if ltp not in (None, "") else None
    except Exception:
        return None


def make_futures_fetcher(expiry=None, weekday: int = 1, exchange: str = "NFO",
                         client_factory=None):
    """``fetch(stock_code) -> LTP float | None`` for the near-MONTH futures contract.

    NIFTY futures are monthly (last Tuesday). Never raises — None degrades the CAS
    card to an honest "futures quote unavailable"."""
    def fetch(stock_code: str = "NIFTY") -> float | None:
        try:
            client = (client_factory or get_breeze_client)()
            exp = _expiry_iso(expiry, weekday, monthly=True)
            resp = client.get_quotes(stock_code=stock_code, exchange_code=exchange,
                                     product_type="futures", expiry_date=exp,
                                     right="others", strike_price="0")
            return parse_futures_ltp(resp)
        except Exception:
            return None

    return fetch
