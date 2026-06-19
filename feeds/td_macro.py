"""Live macro-scorecard quote fetcher for ``feeds.macro``.

Globals (crude, USD/INR, US indices) come from Twelve Data's ``/quote`` endpoint;
India VIX from Breeze. Each returns ``{price, prev_close}`` which
``feeds.macro.summarise_quote`` turns into ``{price, change_pct}``. Per-symbol
failures degrade to None in ``fetch_macro``, so partial data is fine (e.g. GIFT
Nifty, which has no reliable free source, simply drops out).
"""

from __future__ import annotations

import os

_TD_QUOTE_URL = "https://api.twelvedata.com/quote"

# Scorecard name -> source spec. ("td", symbol) = Twelve Data; ("breeze_index",
# code) = Breeze index quote (India VIX). Edit/extend via config feeds.macro.
DEFAULT_SCORECARD = {
    "crude_wti": ("td", "WTI/USD"),
    "usd_inr": ("td", "USD/INR"),
    "us30_dow": ("td", "DJI"),
    "nasdaq": ("td", "IXIC"),
    "india_vix": ("breeze_index", "INDIA VIX"),
}


def _td_quote(symbol: str, api_key: str) -> dict:
    """Twelve Data /quote -> {price, prev_close}."""
    import requests

    resp = requests.get(
        _TD_QUOTE_URL, params={"symbol": symbol, "apikey": api_key}, timeout=15
    )
    resp.raise_for_status()
    p = resp.json()
    if p.get("status") == "error":
        raise RuntimeError(f"Twelve Data error for {symbol!r}: {p.get('message')}")
    return {"price": float(p["close"]), "prev_close": float(p["previous_close"])}


def _breeze_index_quote(code: str, client_factory=None) -> dict:
    """Breeze index quote (e.g. India VIX) -> {price, prev_close}."""
    from loaders.breeze import get_breeze_client

    client = (client_factory or get_breeze_client)()
    resp = client.get_quotes(
        stock_code=code, exchange_code="NSE", product_type="cash"
    )
    if resp.get("Error"):
        raise RuntimeError(f"Breeze quote error for {code!r}: {resp.get('Error')}")
    row = (resp.get("Success") or [{}])[0]
    return {"price": float(row["ltp"]), "prev_close": float(row["previous_close"])}


def make_quote_fn(api_key: str | None = None, client_factory=None):
    """Return ``quote_fn(name) -> {price, prev_close}`` over ``DEFAULT_SCORECARD``.

    ``name`` is the scorecard key (not a raw symbol); the source spec routes it to
    Twelve Data or Breeze. Pass the scorecard via ``feeds.macro.fetch_macro(symbols=)``
    where each value is the scorecard key. Missing keys raise (degraded per-symbol).
    """
    api_key = api_key or os.environ.get("TWELVEDATA_API_KEY")

    def quote_fn(name: str) -> dict:
        kind, code = DEFAULT_SCORECARD[name]
        if kind == "td":
            if not api_key:
                raise RuntimeError("TWELVEDATA_API_KEY not set")
            return _td_quote(code, api_key)
        if kind == "breeze_index":
            return _breeze_index_quote(code, client_factory)
        raise RuntimeError(f"unknown macro source kind: {kind!r}")

    return quote_fn


# fetch_macro(symbols=...) expects {name: symbol}; here name == symbol == scorecard key.
SCORECARD_SYMBOLS = {k: k for k in DEFAULT_SCORECARD}
