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
from indicators.directional import (
    MTFDirectionalConfig, resolve_direction_mtf, mtf_ema45_confidence,
)
from feeds.oi import fetch_oi
from feeds.macro import fetch_macro

# The TF ladder the trader watches. The resolver consumes the 3m trigger + the
# bias set; 1m and 1month are carried for display/context only (monthly EMAs need
# years of history, so they stay out of the per-bar resolver).
_RESAMPLE_FROM_1M = {"3min": "3min", "15min": "15min", "30min": "30min", "60min": "60min"}
_RESAMPLE_FROM_DAILY = {"1week": "1W", "1month": "MS"}
_RESOLVER_TFS = ("3min", "15min", "30min", "60min", "1day", "1week")


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
    calls = resolve_direction_mtf(feats, cfg)
    call = calls.iloc[-1]
    # MTF 45-EMA conviction: how many higher TFs have price on the signal's side.
    conf, align = mtf_ema45_confidence(feats, calls)
    trig = feats["3min"].iloc[-1]
    daily = feats["1day"].iloc[-1]
    # Session low/high so far (the journal's stop basis = today's running extreme).
    f3 = feats["3min"]
    today = f3.index[-1].normalize()
    sess = f3[f3.index.normalize() == today]
    sess_low = _f(sess["low"].min()) if "low" in sess else None
    sess_high = _f(sess["high"].max()) if "high" in sess else None
    return {
        "mtf_call": str(call),
        "regime_45_daily": _sign(daily["close"] - daily["ema_45"]),
        "supertrend_3m": int(trig["st_dir"]),
        "ema5_trigger_3m": int(trig["sig_ema5_trigger"]),
        "mtf_confidence": int(conf.iloc[-1]),
        "mtf_confidence_breakdown": {tf: int(align[tf].iloc[-1]) for tf in align.columns},
        "levels": {
            "ema_45": float(trig["ema_45"]),
            "supertrend": float(trig["supertrend"]),
            "cpr_pivot": _f(trig.get("cpr_pivot")),
            "cpr_tc": _f(trig.get("cpr_tc")),
            "cpr_bc": _f(trig.get("cpr_bc")),
            "session_low": sess_low,
            "session_high": sess_high,
        },
        # Raw chart numbers for the always-visible market-data panel.
        "numbers": {
            "ema_5": _f(trig.get("ema_5")),
            "ema_45": _f(trig.get("ema_45")),
            "ema_100": _f(trig.get("ema_100")),
            "ema_200": _f(trig.get("ema_200")),
            "supertrend": _f(trig.get("supertrend")),
            "rsi_14": _f(trig.get("rsi_14")),
            "macd_hist": _f(trig.get("macd_hist")),
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
    macro_symbols: dict | None = None,
    macro: dict | None = None,
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
    oi = fetch_oi(instrument, spot, oi_fetch_fn, errors=notes)
    if oi is None and oi_fetch_fn is None:
        notes.append("oi: no fetcher configured")
    if macro is None:  # pre-fetched override skips the call (live loop throttles it)
        macro = fetch_macro(symbols=macro_symbols, quote_fn=macro_quote_fn, errors=notes)
        if macro is None and macro_quote_fn is None:
            notes.append("macro: no fetcher configured")

    return Snapshot(
        instrument=instrument, ts=ts, spot=spot, frames=frames, feats=feats,
        chart_read=read, oi=oi, macro=macro, notes=notes,
    )


def build_snapshot_at(
    instrument: str,
    base_1m: pd.DataFrame,
    daily: pd.DataFrame,
    target_ts,
    **kw,
) -> Snapshot:
    """Reconstruct the snapshot AS-OF a past timestamp with no future leakage.

    Truncates the 1-minute base to ``index <= target_ts`` and rebuilds the daily
    series as *completed prior sessions + a partial bar for the target session*
    (built from the truncated intraday) — exactly what the live loop would have held
    at that moment, so today's full-day close never leaks into the daily-based
    regime. The causal indicator/resample stack does the rest. Used by the training
    replay to drive the as-of chart and Claude's read.
    """
    t = pd.Timestamp(target_ts)
    base = base_1m[base_1m.index <= t]
    prior = daily[daily.index.normalize() < t.normalize()]
    day_bars = base[base.index.normalize() == t.normalize()]
    if not day_bars.empty:
        vol = day_bars["volume"].sum() if "volume" in day_bars else 0
        # match the daily series' tz dtype exactly so the concat stays a DatetimeIndex
        tz = daily.index.tz
        day0 = (t.tz_convert(tz) if tz is not None else t).normalize()
        partial = pd.DataFrame(
            {"open": day_bars["open"].iloc[0], "high": day_bars["high"].max(),
             "low": day_bars["low"].min(), "close": day_bars["close"].iloc[-1],
             "volume": vol},
            index=pd.DatetimeIndex([day0], name=daily.index.name))
        d = pd.concat([prior, partial])
    else:
        d = prior
    return build_snapshot(instrument, base, d, **kw)
