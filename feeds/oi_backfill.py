"""Reconstruct the last ~7 days of option-chain OI from Breeze history.

Breeze's historical API returns ``open_interest`` per option instrument, so we can
rebuild the chain at past timestamps: for each trading day, pull each near-ATM
strike's (call & put) historical OI series for that day's weekly expiry, then sample
on a time grid into chain snapshots and save them via ``feeds.oi_store`` — the same
format the live logger writes, so training mode reads both uniformly.

The assembly (``assemble_day``) is pure and tested; the Breeze fetching is a thin,
paced live wrapper (run locally: ``python -m feeds.oi_backfill``). Many calls, so it
sleeps between strikes to respect rate limits.
"""

from __future__ import annotations

import argparse
import time
from datetime import date, datetime, timedelta

import pandas as pd

from feeds.breeze_oi import nearest_weekly
from feeds import oi_store

STRIKE_STEP = 50


def _at_or_before(df: pd.DataFrame, t: pd.Timestamp, col: str):
    if df is None or df.empty or col not in df:
        return None
    s = df.loc[df.index <= t, col]
    return float(s.iloc[-1]) if len(s) else None


def assemble_day(
    strike_series: dict[tuple[float, str], pd.DataFrame],
    index_bars: pd.DataFrame,
    grid_minutes: int = 15,
) -> list[tuple[pd.Timestamp, float, pd.DataFrame]]:
    """Build ``[(ts, spot, chain_df)]`` snapshots from per-strike OI series.

    ``strike_series`` maps ``(strike, "call"|"put") -> DataFrame`` indexed by
    timestamp with ``open_interest`` (+ optional ``close`` for LTP). ``index_bars``
    is the index OHLCV for the day (``close`` = spot). Sampled every ``grid_minutes``.
    """
    if index_bars is None or index_bars.empty:
        return []
    strikes = sorted({k for k, _ in strike_series})
    grid = pd.date_range(index_bars.index[0], index_bars.index[-1],
                         freq=f"{grid_minutes}min")
    snaps = []
    for t in grid:
        spot = _at_or_before(index_bars, t, "close")
        if spot is None:
            continue
        rows = []
        for k in strikes:
            cs, ps = strike_series.get((k, "call")), strike_series.get((k, "put"))
            rows.append({
                "strike": float(k),
                "call_oi": _at_or_before(cs, t, "open_interest") or 0.0,
                "put_oi": _at_or_before(ps, t, "open_interest") or 0.0,
                "call_ltp": _at_or_before(cs, t, "close"),
                "put_ltp": _at_or_before(ps, t, "close"),
            })
        snaps.append((t, spot, pd.DataFrame(rows)))
    return snaps


def _iso(d: date, end: bool) -> str:
    h, m, s = (15, 30, 0) if end else (9, 15, 0)
    return datetime(d.year, d.month, d.day, h, m, s).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _rows(resp: dict) -> pd.DataFrame:
    data = (resp or {}).get("Success") or []
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    idx = pd.to_datetime(df["datetime"])
    if idx.dt.tz is None:                       # Breeze returns naive IST
        idx = idx.dt.tz_localize("Asia/Kolkata")
    df["datetime"] = idx
    return df.set_index("datetime").sort_index()


def fetch_day_series(client, symbol, day, expiry_iso, strikes, interval):
    """Pull per-strike call/put historical OI for one day (paced)."""
    out = {}
    for k in strikes:
        for right in ("call", "put"):
            resp = client.get_historical_data_v2(
                interval=interval, from_date=_iso(day, False), to_date=_iso(day, True),
                stock_code=symbol, exchange_code="NFO", product_type="options",
                expiry_date=expiry_iso, right=right, strike_price=int(k))
            out[(float(k), right)] = _rows(resp)
            time.sleep(0.3)
    return out


def backfill(symbol="NIFTY", days=7, grid_minutes=15, n_strikes=20,
             interval="5minute", weekday=1, client=None, index_for_day=None,
             base=oi_store.DATA_DIR) -> int:
    """Reconstruct + store OI snapshots for the last ``days`` trading days.

    ``client`` (Breeze) and ``index_for_day(day) -> index OHLCV DataFrame`` are
    injectable for tests; live defaults build them from the env creds + loader.
    Returns the number of snapshots saved.
    """
    if client is None:
        from loaders.breeze import get_breeze_client
        client = get_breeze_client()
    if index_for_day is None:
        from loaders import get_loader
        loader = get_loader("breeze")

        def index_for_day(day):
            bars = loader.load(symbol, "minute", start=day, end=day, use_cache=False)
            return bars[bars.index.date == day]

    saved = 0
    today = date.today()
    trading_days = [d for i in range(1, days * 2)
                    if (d := today - timedelta(days=i)).weekday() < 5][:days]
    for day in sorted(trading_days):
        index_bars = index_for_day(day)
        if index_bars is None or index_bars.empty:
            continue
        atm = round(float(index_bars["close"].iloc[-1]) / STRIKE_STEP) * STRIKE_STEP
        strikes = [atm + i * STRIKE_STEP for i in range(-n_strikes, n_strikes + 1)]
        expiry = nearest_weekly(weekday, day)
        expiry_iso = datetime(expiry.year, expiry.month, expiry.day,
                              6, 0, 0).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        series = fetch_day_series(client, symbol, day, expiry_iso, strikes, interval)
        for ts, spot, chain in assemble_day(series, index_bars, grid_minutes):
            oi_store.save_chain(symbol, ts, spot, chain, base=base)
            saved += 1
    return saved


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--grid", type=int, default=15, help="snapshot grid (minutes)")
    ap.add_argument("--strikes", type=int, default=20, help="± strikes around ATM")
    ap.add_argument("--weekday", type=int, default=1, help="expiry weekday (Tue=1)")
    a = ap.parse_args(argv)
    saved = backfill(a.symbol, a.days, a.grid, a.strikes, weekday=a.weekday)
    print(f"saved {saved} OI snapshots to {oi_store.DATA_DIR}/{a.symbol}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
