"""Build ONE market snapshot for an instrument: multi-TF OHLCV + chart read,
plus the optional OI and macro context.

The chart read reuses the Track-2 core unchanged: ``resample_ohlcv`` for the TF
ladder, ``build_mtf_features`` for indicators, ``resolve_direction_mtf`` for the
single long/short/flat call. OI/macro attach via injected fetchers and degrade
to None. Live pulls run on the user's machine; the sandbox uses a mock loader.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from indicators.timeframes import resample_ohlcv, build_mtf_features
from indicators.directional import MTFDirectionalConfig, resolve_direction_mtf
from feeds.oi import fetch_oi
from feeds.macro import fetch_macro

# The TF ladder the trader watches. The resolver consumes the 3m trigger + the
# bias set; 1m and 1month are carried for display/context only (monthly EMAs need
# years of history, so they stay out of the per-bar resolver).
_RESAMPLE_FROM_1M = {"3min": "3min", "15min": "15min", "60min": "60min"}
_RESAMPLE_FROM_DAILY = {"1week": "1W", "1month": "MS"}
_RESOLVER_TFS = ("3min", "15min", "60min", "1day", "1week")


@dataclass
class Snapshot:
    instrument: str
    ts: str
    spot: float
    frames: dict[str, pd.DataFrame]                 # every TF, OHLCV
    feats: dict[str, pd.DataFrame]                   # resolver TFs, with indicators
    chart_read: dict                                 # call + regime + levels
    oi: dict | None = None
    macro: dict | None = None
    notes: list[str] = field(default_factory=list)   # degradation flags


def assemble_ladder(
    base_1m: pd.DataFrame, daily: pd.DataFrame, anchor: str | None = None
) -> dict[str, pd.DataFrame]:
    """Build the full TF ladder from a 1-minute base + a daily series."""
    frames: dict[str, pd.DataFrame] = {"1min": base_1m, "1day": daily}
    for tf, rule in _RESAMPLE_FROM_1M.items():
        frames[tf] = resample_ohlcv(base_1m, rule, anchor)
    for tf, rule in _RESAMPLE_FROM_DAILY.items():
        frames[tf] = resample_ohlcv(daily, rule)
    return frames


def _chart_read(feats: dict[str, pd.DataFrame], cfg: MTFDirectionalConfig) -> dict:
    """Resolve the MTF call and pull the last-bar levels the analysis layer needs."""
    call = resolve_direction_mtf(feats, cfg).iloc[-1]
    trig = feats["3min"].iloc[-1]
    daily = feats["1day"].iloc[-1]
    return {
        "mtf_call": str(call),
        "regime_45_daily": _sign(daily["close"] - daily["ema_45"]),
        "supertrend_3m": int(trig["st_dir"]),
        "ema5_trigger_3m": int(trig["sig_ema5_trigger"]),
        "levels": {
            "ema_45": float(trig["ema_45"]),
            "supertrend": float(trig["supertrend"]),
            "cpr_pivot": _f(trig.get("cpr_pivot")),
            "cpr_tc": _f(trig.get("cpr_tc")),
            "cpr_bc": _f(trig.get("cpr_bc")),
        },
    }


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _f(x) -> float | None:
    try:
        return None if pd.isna(x) else float(x)
    except (TypeError, ValueError):
        return None


def build_snapshot(
    instrument: str,
    base_1m: pd.DataFrame,
    daily: pd.DataFrame,
    anchor: str | None = None,
    mtf_cfg: MTFDirectionalConfig | None = None,
    indicator_params: dict | None = None,
    oi_fetch_fn: Callable | None = None,
    macro_quote_fn: Callable | None = None,
) -> Snapshot:
    """Assemble the snapshot from already-pulled 1m + daily frames.

    Pulling (Breeze 1minute + 1day) happens upstream so this stays offline-testable
    — pass mock frames here. OI/macro fetchers are injected and optional.
    """
    cfg = mtf_cfg or MTFDirectionalConfig()
    frames = assemble_ladder(base_1m, daily, anchor)
    feats = build_mtf_features(
        {tf: frames[tf] for tf in _RESOLVER_TFS}, indicator_params
    )

    spot = float(frames["3min"]["close"].iloc[-1])
    ts = frames["3min"].index[-1].isoformat()
    read = _chart_read(feats, cfg)

    notes: list[str] = []
    oi = fetch_oi(instrument, spot, oi_fetch_fn)
    if oi is None:
        notes.append("oi: unavailable (no fetcher / pull failed) — degraded")
    macro = fetch_macro(quote_fn=macro_quote_fn)
    if macro is None:
        notes.append("macro: unavailable (no fetcher) — degraded")

    return Snapshot(
        instrument=instrument, ts=ts, spot=spot, frames=frames, feats=feats,
        chart_read=read, oi=oi, macro=macro, notes=notes,
    )
