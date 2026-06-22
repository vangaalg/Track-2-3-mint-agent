"""The trader's daily overlay — GIFT Nifty (manual) + the overnight-events note.

These are once-a-morning, discretionary inputs (not a 15-min series), so they live as a small JSON
blob (``data/context.json``) the recorder reads each cycle, rather than in the numeric macro parquet.
The events note is the text Claude produced from a GIFT/news screenshot (the trader's actual method);
the manual GIFT value is the source of truth when investing.com is blocked.
"""

from __future__ import annotations

import json
from pathlib import Path

STORE_PATH = Path("data/context.json")


def _path(root: str | Path | None) -> Path:
    return (Path(root) / "context.json") if root else STORE_PATH


def load_context(root: str | Path | None = None) -> dict:
    """Return the saved overlay, or empty defaults if none set yet."""
    p = _path(root)
    if not p.exists():
        return {"gift_manual": None, "events_note": "", "set_at": None}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"gift_manual": None, "events_note": "", "set_at": None}


def save_context(gift_manual=None, events_note=None, ts=None,
                 root: str | Path | None = None) -> dict:
    """Patch + persist the overlay (only the fields given are updated). Returns the new state."""
    import pandas as pd
    cur = load_context(root)
    if gift_manual is not None:
        cur["gift_manual"] = _num(gift_manual)
    if events_note is not None:
        cur["events_note"] = str(events_note)
    cur["set_at"] = str(ts) if ts is not None else pd.Timestamp.now(tz="Asia/Kolkata").isoformat()
    p = _path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur), encoding="utf-8")
    return cur


def _num(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None
