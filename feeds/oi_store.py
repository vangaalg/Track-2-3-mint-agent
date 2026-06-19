"""On-disk store of option-chain OI snapshots — the data flywheel for training.

OI as-it-was at a past moment is the one piece we can't cheaply re-derive (chart
OHLCV is always re-pullable from Breeze history), so we persist every fetched
chain. One parquet per snapshot under ``data/oi/<symbol>/`` named by IST timestamp;
``load_nearest`` returns the chain at/just-before a target time (for training mode).

Snapshot frame columns: strike, call_oi, put_oi, call_ltp, put_ltp (+ ts, spot).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

DATA_DIR = Path("data/oi")
_FMT = "%Y%m%dT%H%M%S"


def _ist_naive(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tz is not None:
        t = t.tz_convert("Asia/Kolkata").tz_localize(None)
    return t


def save_chain(symbol: str, ts, spot, chain: pd.DataFrame,
               base: str | Path = DATA_DIR) -> Path:
    """Persist one chain snapshot. Idempotent per (symbol, minute)."""
    d = Path(base) / symbol
    d.mkdir(parents=True, exist_ok=True)
    t = _ist_naive(ts)
    path = d / (t.strftime(_FMT) + ".parquet")
    out = chain.copy()
    out["ts"] = pd.Timestamp(ts).isoformat()
    out["spot"] = spot
    out.to_parquet(path)
    return path


def list_snapshots(symbol: str, base: str | Path = DATA_DIR) -> list[tuple[pd.Timestamp, Path]]:
    d = Path(base) / symbol
    if not d.exists():
        return []
    items = []
    for f in d.glob("*.parquet"):
        try:
            items.append((pd.Timestamp(datetime.strptime(f.stem, _FMT)), f))
        except ValueError:
            continue
    return sorted(items)


def load_nearest(symbol: str, ts, base: str | Path = DATA_DIR) -> pd.DataFrame | None:
    """Chain snapshot at or just before ``ts`` (None if none earlier)."""
    target = _ist_naive(ts)
    best = None
    for t, f in list_snapshots(symbol, base):
        if t <= target:
            best = f
        else:
            break
    return pd.read_parquet(best) if best is not None else None
