"""Daily F&O liquidity ranking (PURE).

"Most liquid that day" = ranked by traded VALUE (close × volume), computed from the
scanner's already-pulled daily OHLCV (``feeds.scanner._day_stats`` on each row) — so it
costs no extra Breeze pulls. The OI-buildup watchlist shows the day's top-N liquid
stocks alongside the indices; the ranking refreshes as the day's volume accrues.
"""

from __future__ import annotations


def _value(row: dict) -> float | None:
    """Traded value = close × volume (₹ turnover proxy). None when either is missing."""
    close, vol = row.get("close"), row.get("volume")
    if close is None or vol is None:
        return None
    try:
        return float(close) * float(vol)
    except (TypeError, ValueError):
        return None


def rank_by_liquidity(rows: list[dict], n: int = 20) -> list[str]:
    """Top-``n`` stock symbols by traded value (desc). Rows are scanner rows carrying
    ``symbol`` + day-stats (``close``/``volume``). Symbols with no value fall to the
    end (kept, so the list still fills when volume hasn't loaded), ordered by symbol."""
    ranked = []
    for r in rows or []:
        sym = r.get("symbol")
        if not sym:
            continue
        ranked.append((sym, _value(r)))
    # value present first (desc), then the rest alphabetically for a stable order
    ranked.sort(key=lambda x: (x[1] is None, -(x[1] or 0.0), x[0]))
    return [sym for sym, _ in ranked[:max(0, n)]]
