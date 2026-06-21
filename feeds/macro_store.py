"""Persistent macro time-series store — the forward macro flywheel (mirrors ohlcv_store).

`feeds.macro.fetch_macro` returns the live scorecard as a transient dict; nothing
persisted it. Here we append each fetch as one timestamped row to a single growing
parquet (`data/macro/macro.parquet`), dedup on ts, so VIX / USD-INR / US indices /
crude accumulate over time and can be plotted or joined to triggers later.

Pure + offline-testable (no network here).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

STORE_DIR = Path("data/macro")


def store_path(root: str | Path | None = None) -> Path:
    return (Path(root) if root else STORE_DIR) / "macro.parquet"


def _flatten(macro: dict | None, ts) -> dict:
    """One row: ts + <name>_price / <name>_change for each scorecard entry."""
    row = {"ts": pd.Timestamp(ts).isoformat()}
    for name, v in (macro or {}).items():
        row[f"{name}_price"] = (v or {}).get("price") if isinstance(v, dict) else None
        row[f"{name}_change"] = (v or {}).get("change_pct") if isinstance(v, dict) else None
    return row


def load_macro(root: str | Path | None = None) -> pd.DataFrame | None:
    path = store_path(root)
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    return df[~df.index.duplicated(keep="last")].sort_index()


def append_macro(macro: dict | None, ts, root: str | Path | None = None) -> pd.DataFrame:
    """Append one macro snapshot, dedup on ts (newest wins), persist, return combined."""
    new = pd.DataFrame([_flatten(macro, ts)]).set_index("ts")
    existing = load_macro(root)
    combined = new if existing is None else pd.concat([existing, new])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    path = store_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(path)
    return combined
