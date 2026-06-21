"""Persistent local OHLCV store — pull from Breeze ONCE, accumulate for years.

The backtest used to re-pull the whole window from Breeze every run (slow + at the
mercy of the network). Instead we keep a growing parquet per ``(symbol, interval)``
under ``data/ohlcv/`` and:

  * **merge** every fresh pull into it (dedup on timestamp, keep newest), so each
    online run EXTENDS the history — over time you build years of bars locally;
  * serve backtests from the store, pulling only the **gap** since the last stored
    bar (or nothing at all in ``--offline`` mode → instant, no network).

Pure + import-clean (no network here); the networked pull lives in the backtest CLI.
``data/ohlcv/`` is gitignored.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

STORE_DIR = Path("data/ohlcv")


def store_path(symbol: str, interval: str, root: str | Path | None = None) -> Path:
    safe = symbol.replace("/", "-").replace(" ", "_")
    return (Path(root) if root else STORE_DIR) / f"{safe}_{interval}.parquet"


def load_ohlcv(symbol: str, interval: str, root: str | Path | None = None) -> pd.DataFrame | None:
    """Return the full stored frame for ``(symbol, interval)`` or ``None`` if absent."""
    p = store_path(symbol, interval, root)
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    return df[~df.index.duplicated(keep="last")].sort_index()


def merge_save(symbol: str, interval: str, fresh: pd.DataFrame,
               root: str | Path | None = None) -> pd.DataFrame:
    """Merge ``fresh`` into the stored frame (newest wins on duplicate timestamps),
    persist, and return the combined frame. Empty ``fresh`` is a no-op read-through.
    """
    existing = load_ohlcv(symbol, interval, root)
    if fresh is None or fresh.empty:
        combined = existing if existing is not None else fresh
    elif existing is None or existing.empty:
        combined = fresh
    else:
        combined = pd.concat([existing, fresh])
    if combined is None or combined.empty:
        return combined
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    p = store_path(symbol, interval, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(p)
    return combined


def coverage(symbol: str, interval: str, root: str | Path | None = None):
    """``(first_ts, last_ts, n_bars)`` of the store, or ``None`` if empty — used to
    decide which gap to pull and to report how much history is banked."""
    df = load_ohlcv(symbol, interval, root)
    if df is None or df.empty:
        return None
    return df.index.min(), df.index.max(), len(df)
