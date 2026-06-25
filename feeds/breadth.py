"""NIFTY-50 market breadth + index-contribution (PURE).

Turns the scanner's free 50-stock snapshots (each already carries today's OHLCV + %-change,
see ``feeds.scanner._day_stats``) into an intraday DIRECTION read:

  • advance / decline / unchanged tally across all 50 constituents (e.g. 40:10), and
  • the top-N heavyweights with their point-contribution to NIFTY today
    (contribution ≈ weight × %-move × index-level — Σ ≈ today's NIFTY basket move).

No I/O — `compute_breadth` is a pure function over the scan rows + the live NIFTY spot, so it's
fully offline-testable. Index weights are a STATIC table (below): approximate free-float weights,
good enough for a direction read — refresh every few months as NSE rebalances the index.
"""

from __future__ import annotations

# Approximate NIFTY-50 free-float weights (%) — keyed to feeds.instruments.NSE50_STOCKS.
# APPROXIMATE + needs a periodic manual refresh (NSE rebalances ~semi-annually). Exact values
# aren't critical: the SIGN and relative magnitude of each contribution drive the direction read.
NIFTY50_WEIGHTS: dict[str, float] = {
    "HDFCBANK": 13.0, "ICICIBANK": 8.5, "RELIANCE": 8.0, "INFY": 6.0, "TCS": 4.0,
    "ITC": 4.0, "BHARTIARTL": 4.0, "LT": 3.8, "AXISBANK": 3.2, "SBIN": 3.0,
    "KOTAKBANK": 2.6, "BAJFINANCE": 2.4, "HINDUNILVR": 2.2, "M&M": 2.0, "MARUTI": 1.9,
    "SUNPHARMA": 1.8, "NTPC": 1.7, "TATAMOTORS": 1.6, "HCLTECH": 1.5, "ULTRACEMCO": 1.3,
    "TITAN": 1.3, "POWERGRID": 1.2, "ASIANPAINT": 1.1, "BAJAJFINSV": 1.1, "ADANIENT": 1.0,
    "ADANIPORTS": 1.0, "ONGC": 1.0, "COALINDIA": 1.0, "WIPRO": 0.9, "NESTLEIND": 0.9,
    "JSWSTEEL": 0.9, "BAJAJ-AUTO": 0.9, "TATASTEEL": 0.8, "TECHM": 0.8, "GRASIM": 0.8,
    "HINDALCO": 0.8, "SBILIFE": 0.7, "HDFCLIFE": 0.7, "INDUSINDBK": 0.7, "CIPLA": 0.7,
    "DRREDDY": 0.7, "SHRIRAMFIN": 0.6, "EICHERMOT": 0.6, "BRITANNIA": 0.6, "APOLLOHOSP": 0.6,
    "HEROMOTOCO": 0.5, "BPCL": 0.5, "DIVISLAB": 0.5, "TATACONSUM": 0.5, "LTIM": 0.5,
}


def _num(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if x != x else x          # NaN-safe


def compute_breadth(scan_rows, nifty_spot=None, top_n: int = 20) -> dict:
    """Breadth + contribution from the scanner rows (each `{symbol, pct_change, open, high, low,
    close, volume, ...}`) and the live NIFTY spot.

    Advance/decline counts EVERY stock with a numeric %-change. Contribution (index points) is
    `(weight/100) * (pct/100) * nifty_spot` — `None` when the weight or the spot is missing. The
    table is the `top_n` symbols BY WEIGHT (the heavyweights), shown sorted by signed contribution.
    """
    spot = _num(nifty_spot)
    adv = dec = unch = 0
    enriched: list[dict] = []
    net = 0.0
    has_net = False
    for r in scan_rows or []:
        pct = _num(r.get("pct_change"))
        if pct is None:
            continue
        if pct > 0:
            adv += 1
        elif pct < 0:
            dec += 1
        else:
            unch += 1
        sym = r.get("symbol")
        w = NIFTY50_WEIGHTS.get(sym)
        if w is None:                     # counted in A/D, but no weight → not an index contributor
            continue
        contrib = None
        if spot:
            contrib = (w / 100.0) * (pct / 100.0) * spot
            net += contrib
            has_net = True
        close = _num(r.get("close"))
        if close is None:
            close = _num(r.get("spot"))
        enriched.append({
            "symbol": sym, "weight": w,
            "open": _num(r.get("open")), "high": _num(r.get("high")), "low": _num(r.get("low")),
            "close": close, "volume": _num(r.get("volume")), "pct_change": round(pct, 2),
            "contribution": round(contrib, 1) if contrib is not None else None,
        })

    top = sorted(enriched, key=lambda x: x["weight"], reverse=True)[:top_n]
    top.sort(key=lambda x: (x["contribution"] if x["contribution"] is not None else x["pct_change"]),
             reverse=True)                # display: biggest positive contribution first
    return {"rows": top, "advance": adv, "decline": dec, "unchanged": unch,
            "total": adv + dec + unch, "net_points": round(net, 1) if has_net else None}
