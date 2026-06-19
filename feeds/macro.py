"""Macro scorecard — the morning-context feeds (Phase 1: fetch + display only).

The journal's scorecard: GIFT Nifty, crude (Brent/WTI), USD/INR, US30 futures,
Nasdaq, Dow, India VIX. Globals come from Twelve Data; India-specific (India VIX,
GIFT) from NSE. Here we only *fetch the latest mark + day change* and hand it
back as a dict for the dashboard — it is **not** modelled into the signal yet
(that is Phase 2). Injectable fetcher → testable offline; degrades to None.
"""

from __future__ import annotations

from typing import Callable

# name -> provider symbol (the globals we read via the Twelve Data loader).
DEFAULT_GLOBALS = {
    "crude_wti": "WTI/USD",
    "usd_inr": "USD/INR",
    "us30": "DJI",
    "nasdaq": "IXIC",
    "dow": "DJI",
}


def summarise_quote(latest: dict) -> dict:
    """Normalise one quote ``{price, prev_close}`` → ``{price, change_pct}``."""
    price = float(latest["price"])
    prev = latest.get("prev_close")
    change_pct = (
        float((price - prev) / prev * 100.0) if prev not in (None, 0) else None
    )
    return {"price": price, "change_pct": change_pct}


def fetch_macro(
    symbols: dict[str, str] | None = None,
    quote_fn: Callable[[str], dict] | None = None,
) -> dict | None:
    """Fetch the scorecard. Returns None (degrade) if no fetcher is supplied.

    ``quote_fn(symbol) -> {"price": float, "prev_close": float}`` is injected (a
    Twelve Data / NSE adapter). Per-symbol failures degrade to None for that name;
    the rest still populate.
    """
    if quote_fn is None:
        return None
    symbols = symbols or DEFAULT_GLOBALS
    out: dict[str, dict | None] = {}
    for name, sym in symbols.items():
        try:
            out[name] = summarise_quote(quote_fn(sym))
        except Exception:
            out[name] = None
    return out
