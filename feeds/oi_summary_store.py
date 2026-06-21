"""Per-instrument OI summary time series — PCR / max-pain / walls / S/R bands.

The full chain snapshots live in ``feeds.oi_store`` (deep analysis). This is the
compact, plot-ready companion: one row per recording cycle per instrument with the
numbers the trader watches on a line graph (PCR, max-pain) plus the wall strikes and
the extension bands. Grows as parquet under ``data/oi_summary/<symbol>.parquet``.

Pure + offline-testable.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

STORE_DIR = Path("data/oi_summary")


def store_path(symbol: str, root: str | Path | None = None) -> Path:
    safe = symbol.replace("/", "-").replace(" ", "_")
    return (Path(root) if root else STORE_DIR) / f"{safe}.parquet"


def load_summary(symbol: str, root: str | Path | None = None) -> pd.DataFrame | None:
    path = store_path(symbol, root)
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    return df[~df.index.duplicated(keep="last")].sort_index()


def _row(ts, spot, summary: dict, levels: dict) -> dict:
    cw = (summary or {}).get("call_wall") or {}
    ps = (summary or {}).get("put_shelf") or {}
    res_ext = (levels or {}).get("resistance_ext") or []
    sup_ext = (levels or {}).get("support_ext") or []
    pick = lambda seq, i: seq[i] if len(seq) > i else None
    return {
        "ts": pd.Timestamp(ts).isoformat(),
        "spot": spot,
        "pcr": (summary or {}).get("pcr"),
        "max_pain": (summary or {}).get("max_pain"),
        "atm": (summary or {}).get("atm"),
        "call_wall_strike": cw.get("strike"),
        "call_wall_oi": cw.get("oi"),
        "put_shelf_strike": ps.get("strike"),
        "put_shelf_oi": ps.get("oi"),
        "res_ext1": pick(res_ext, 0), "res_ext2": pick(res_ext, 1),
        "sup_ext1": pick(sup_ext, 0), "sup_ext2": pick(sup_ext, 1),
    }


def append_summary(symbol: str, ts, spot, summary: dict, levels: dict,
                   root: str | Path | None = None) -> pd.DataFrame:
    """Append one summary row, dedup on ts (newest wins), persist, return combined."""
    new = pd.DataFrame([_row(ts, spot, summary, levels)]).set_index("ts")
    existing = load_summary(symbol, root)
    combined = new if existing is None else pd.concat([existing, new])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    path = store_path(symbol, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(path)
    return combined
