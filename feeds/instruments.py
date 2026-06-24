"""Tradeable-instrument registry — the cockpit's per-instrument config (PURE).

One source of truth for everything that differs between underlyings: the loader /
Breeze symbol, the option lot size (₹ multiplier), the expiry weekday + whether it's
monthly-only (no weekly options), and the extension-band offsets. The recorder keeps a
parallel ``DEFAULT_INSTRUMENTS`` list; this is the cockpit-facing view (adds ``lot_size``
+ ``label``) and is reused by ``web.server`` to drive multi-instrument support.

NSE-50 option stocks slot in here later with their own Breeze codes + weekly/last-day
expiry; for now the verified indices are NIFTY (weekly, ±37/72 bands) and Bank Nifty
(monthly last-Tuesday, price-scaled bands).
"""

from __future__ import annotations

from feeds.oi_levels import scaled_offsets, NIFTY_BANDS

# label        : UI name
# loader_symbol: symbol passed to the OHLCV loader + the Breeze option chain
# exchange     : NFO (NSE F&O) / BFO (BSE F&O)
# lot_size     : contract multiplier (₹ P&L + position sizing). Trader-confirmed.
# weekday      : expiry weekday (Mon=0 … so Tue=1)
# monthly      : True when there are NO weekly options (expiry = month's last weekday)
# band         : extension offsets — a fixed list (NIFTY) or "scale" (price-scaled)
# primary      : shown in the cockpit's instrument dropdown (indices); stocks are reached
#                via the scanner, so they're registered (resolvable) but not primary.
INSTRUMENTS: dict[str, dict] = {
    "NIFTY": {
        "label": "NIFTY", "loader_symbol": "NIFTY", "exchange": "NFO",
        "lot_size": 65, "weekday": 1, "monthly": False, "band": list(NIFTY_BANDS),
        "primary": True,
    },
    "BANKNIFTY": {
        "label": "Bank Nifty", "loader_symbol": "CNXBAN", "exchange": "NFO",
        "lot_size": 30, "weekday": 1, "monthly": True, "band": "scale",
        "primary": True,
    },
}

# NSE-50 option stocks for the scanner. Registered so get_instrument() / the cockpit can
# resolve any of them, but NOT primary (they don't clutter the index dropdown). lot_size=1
# is a PLACEHOLDER — exact per-stock F&O lots are deferred (the scanner is points-based);
# the Breeze codes + the monthly-expiry weekday (3 = Thu, provisional) need live confirm.
NSE50_STOCKS = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "ITC", "LT", "AXISBANK",
    "SBIN", "BHARTIARTL", "KOTAKBANK", "HINDUNILVR", "BAJFINANCE", "ASIANPAINT",
    "MARUTI", "TITAN", "SUNPHARMA", "TATAMOTORS", "NTPC", "POWERGRID", "ULTRACEMCO",
    "NESTLEIND", "WIPRO", "TATASTEEL", "HCLTECH", "ADANIENT", "ADANIPORTS", "JSWSTEEL",
    "M&M", "BAJAJFINSV", "ONGC", "COALINDIA", "GRASIM", "HINDALCO", "INDUSINDBK",
    "TECHM", "CIPLA", "DRREDDY", "BRITANNIA", "EICHERMOT", "APOLLOHOSP", "BPCL",
    "DIVISLAB", "HEROMOTOCO", "TATACONSUM", "BAJAJ-AUTO", "SBILIFE", "HDFCLIFE",
    "LTIM", "SHRIRAMFIN",
]
for _s in NSE50_STOCKS:
    INSTRUMENTS.setdefault(_s, {
        "label": _s.title(), "loader_symbol": _s, "exchange": "NFO",
        "lot_size": 1, "weekday": 3, "monthly": True, "band": "scale", "primary": False,
    })

DEFAULT_INSTRUMENT = "NIFTY"


def get_instrument(symbol: str | None) -> dict:
    """Resolve an instrument config (case-insensitive); falls back to NIFTY."""
    return INSTRUMENTS.get((symbol or DEFAULT_INSTRUMENT).upper(), INSTRUMENTS[DEFAULT_INSTRUMENT])


def offsets_for(inst: dict, spot) -> list[float]:
    """Extension-band offsets for an instrument: its fixed list, or price-scaled from NIFTY."""
    band = inst.get("band")
    if band == "scale" or band is None:
        return scaled_offsets(spot)
    return list(band)


def instrument_list() -> list[dict]:
    """Compact [{id, label}] of PRIMARY instruments for the cockpit's index dropdown
    (stocks are registered but reached via the scanner, not the dropdown)."""
    return [{"id": sym, "label": cfg["label"]} for sym, cfg in INSTRUMENTS.items()
            if cfg.get("primary")]


def scanner_symbols() -> list[str]:
    """The NSE-50 option stocks the scanner screens (non-primary registry entries)."""
    return [sym for sym, cfg in INSTRUMENTS.items() if not cfg.get("primary")]
