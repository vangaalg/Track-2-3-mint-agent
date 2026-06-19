"""Validate our indicator engine against a charting-platform export.

The trader's platform (TradingView-style) can export OHLC + every indicator
column for a symbol/timeframe. This module parses such an export, maps its
verbose headers onto our engine's column names, and compares the two — so we can
prove our EMA/BB/RSI/MACD/Supertrend/CPR math reproduces the chart the trader
actually reads, and catch silent formula drift.

Two comparison modes:

* **From-scratch** (`compare`): run ``compute_indicators`` on the export's OHLC
  and diff against the platform columns. Honest only on a *full-history* export —
  trend indicators (EMA-200, Supertrend, RSI, Bollinger) need their warm-up
  lookback, which a short tail-slice does not contain.
* **Seeded recurrence** (`seed_recurrence`): seed a recursive indicator (any EMA,
  or the MACD signal line) from the platform's first value and step it forward
  with our alpha convention. This validates the *formula* even on a short slice
  with no warm-up — it is what the bundled fixture test uses.

CLI: ``python -m scoring.validate_export <export.csv>`` prints a per-column
error report.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from indicators.engine import compute_indicators

# Platform header (leading plain text / substring) -> our column name.
# Headers carry zero-width joiners and verbose param strings, so we match on
# stable substrings rather than the exact text.
_OHLC = {"open": "open", "high": "high", "low": "low", "close": "close"}


def _map_header(h: str) -> str | None:
    """Map one platform header onto our column name (or None to ignore)."""
    low = h.strip().lower()
    if low in _OHLC:
        return _OHLC[low]
    if low.startswith("bollinger bands top"):
        return "bb_upper"
    if low.startswith("bollinger bands median"):
        return "bb_mid"
    if low.startswith("bollinger bands bottom"):
        return "bb_lower"
    if "(100,ema" in low:
        return "ema_100"
    if "(45,ema" in low:
        return "ema_45"
    if "(200,ema" in low:
        return "ema_200"
    if "(5,ema" in low:
        return "ema_5"
    if "supertrend" in low or low.startswith("trend"):
        return "supertrend"
    if low.startswith("rsi"):
        return "rsi_14"
    if low.startswith("cpr pivot"):
        return "cpr_pivot"
    if low.startswith("cpr bc"):
        return "cpr_bc"
    if low.startswith("cpr tc"):
        return "cpr_tc"
    if low.endswith("_hist"):
        return "macd_hist"
    if low.startswith("macd"):
        return "macd"
    if low.startswith("signal"):
        return "macd_signal"
    return None


def _parse_index(dates: pd.Series, tz: str = "Asia/Kolkata") -> pd.DatetimeIndex:
    """Parse the platform's 'Fri Jun 19 2026 11:39:00 GMT+0530 (...)' stamps.

    The offset/zone suffix is stripped and the naive local time is localised to
    ``tz`` (the timestamps are already exchange-local).
    """
    raw = dates.str.replace(r"\s+GMT.*$", "", regex=True)
    idx = pd.to_datetime(raw, format="%a %b %d %Y %H:%M:%S")
    return pd.DatetimeIndex(idx).tz_localize(tz)


def load_export(path: str | Path, tz: str = "Asia/Kolkata"):
    """Parse a platform export.

    Returns ``(ohlcv, platform)``: a canonical OHLCV frame (volume filled 0 — the
    export carries no volume, and no validated indicator uses it) and a frame of
    the platform's indicator columns, both on the same tz-aware index.
    """
    raw = pd.read_csv(path)
    index = _parse_index(raw[raw.columns[0]], tz)

    mapped: dict[str, pd.Series] = {}
    for col in raw.columns[1:]:
        name = _map_header(col)
        if name is None:
            continue
        mapped[name] = pd.to_numeric(raw[col], errors="coerce").to_numpy()

    frame = pd.DataFrame(mapped, index=index)
    ohlcv = frame[["open", "high", "low", "close"]].copy()
    ohlcv["volume"] = 0.0
    platform = frame.drop(columns=["open", "high", "low", "close"])
    return ohlcv, platform


def compare(
    computed: pd.DataFrame, platform: pd.DataFrame, tol: float = 0.05
) -> pd.DataFrame:
    """Per-column max/mean abs error between our output and the platform.

    Only columns present in both are compared. ``within_tol`` flags max_abs<=tol.
    NOTE: meaningful only past each indicator's warm-up — interpret short-export
    EMA/Bollinger/Supertrend rows accordingly.
    """
    rows = []
    for col in platform.columns:
        if col not in computed.columns:
            continue
        diff = (computed[col] - platform[col]).abs().dropna()
        if diff.empty:
            continue
        rows.append(
            {
                "column": col,
                "n": int(diff.size),
                "max_abs": float(diff.max()),
                "mean_abs": float(diff.mean()),
                "within_tol": bool(diff.max() <= tol),
            }
        )
    return pd.DataFrame(rows)


def seed_recurrence(platform: pd.Series, close: pd.Series, alpha: float) -> pd.Series:
    """Step an EMA-style recurrence from the platform's first value.

    ``out[0] = platform[0]``; ``out[t] = out[t-1] + alpha*(close[t] - out[t-1])``.
    For an N-EMA pass ``alpha = 2/(N+1)``; for the MACD signal line pass the MACD
    series as ``close`` and ``alpha = 2/(9+1)``. Returns the stepped series, to be
    compared against ``platform[1:]``.
    """
    p = platform.to_numpy(dtype=float)
    c = close.to_numpy(dtype=float)
    out = [p[0]]
    for t in range(1, len(c)):
        out.append(out[-1] + alpha * (c[t] - out[-1]))
    return pd.Series(out, index=platform.index, name=f"{platform.name}_seeded")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("export", help="path to the platform CSV export")
    ap.add_argument("--tz", default="Asia/Kolkata")
    ap.add_argument("--tol", type=float, default=0.05)
    args = ap.parse_args(argv)

    ohlcv, platform = load_export(args.export, args.tz)
    computed = compute_indicators(ohlcv)
    report = compare(computed, platform, args.tol)

    print(f"Loaded {len(ohlcv)} rows from {args.export}")
    print("From-scratch comparison (interpret past each indicator's warm-up):\n")
    print(report.to_string(index=False) if not report.empty else "  (no overlap)")
    print(
        "\nNote: EMA/Bollinger/Supertrend/RSI need their lookback — on a short "
        "tail-slice use the seeded-recurrence check (see tests)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
