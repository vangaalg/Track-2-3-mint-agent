"""Indicator engine — reusable, instrument-agnostic.

Every function takes an OHLCV ``pandas.DataFrame`` (columns: ``open``, ``high``,
``low``, ``close``, ``volume``; a ``DatetimeIndex`` is expected but not required
by the math) and returns either a ``Series`` or new columns. The *same* code
runs on Nifty, Nikkei, USD/INR or any US equity — only the upstream data source
differs per market. That portability is the point of Track 2.

The classic indicators (EMA / SMA / Bollinger / RSI / MACD) are fully
implemented. The "3-min strategy" components are structured stubs: the
*shape* of the signal is laid out, but the exact thresholds/logic come from the
journal-derived rules in Phase 2 and are intentionally left as TODOs here.
"""

from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd

OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def _require_columns(df: pd.DataFrame, cols: Iterable[str]) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(
            f"OHLCV frame missing required column(s): {missing}. "
            f"Got columns: {list(df.columns)}"
        )


# --------------------------------------------------------------------------- #
# Moving averages
# --------------------------------------------------------------------------- #
def ema(df: pd.DataFrame, period: int, source: str = "close") -> pd.Series:
    """Exponential moving average of ``source`` over ``period`` bars."""
    _require_columns(df, [source])
    return df[source].ewm(span=period, adjust=False).mean().rename(f"ema_{period}")


def sma(df: pd.DataFrame, period: int, source: str = "close") -> pd.Series:
    """Simple moving average of ``source`` over ``period`` bars.

    Note: SMA-200 needs ~400 bars/days of warm-up history per symbol — pull a
    long enough window upstream or the leading values will be NaN.
    """
    _require_columns(df, [source])
    return df[source].rolling(window=period).mean().rename(f"sma_{period}")


# --------------------------------------------------------------------------- #
# Bollinger Bands
# --------------------------------------------------------------------------- #
def bollinger_bands(
    df: pd.DataFrame,
    period: int = 20,
    num_std: float = 2.0,
    source: str = "close",
) -> pd.DataFrame:
    """Bollinger Bands: middle (SMA), upper, lower, %B and bandwidth.

    Returns a DataFrame with columns ``bb_mid``, ``bb_upper``, ``bb_lower``,
    ``bb_pctb`` (position within the band, 0=lower 1=upper) and ``bb_width``
    (normalised band width — useful for the VRL "squeeze then expand" read).
    """
    _require_columns(df, [source])
    mid = df[source].rolling(window=period).mean()
    std = df[source].rolling(window=period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    width = (upper - lower) / mid
    pctb = (df[source] - lower) / (upper - lower)
    return pd.DataFrame(
        {
            "bb_mid": mid,
            "bb_upper": upper,
            "bb_lower": lower,
            "bb_pctb": pctb,
            "bb_width": width,
        }
    )


# --------------------------------------------------------------------------- #
# RSI
# --------------------------------------------------------------------------- #
def rsi(df: pd.DataFrame, period: int = 14, source: str = "close") -> pd.Series:
    """Wilder's RSI over ``period`` bars."""
    _require_columns(df, [source])
    delta = df[source].diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder smoothing == EWM with alpha = 1/period
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss
    out = 100.0 - (100.0 / (1.0 + rs))
    # When avg_loss == 0 the ratio is inf -> RSI 100; when both 0 -> NaN->50.
    out = out.where(avg_loss != 0, 100.0)
    return out.rename(f"rsi_{period}")


# --------------------------------------------------------------------------- #
# MACD
# --------------------------------------------------------------------------- #
def macd(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    source: str = "close",
) -> pd.DataFrame:
    """MACD line, signal line, and histogram.

    Columns: ``macd``, ``macd_signal``, ``macd_hist``.
    """
    _require_columns(df, [source])
    fast_ema = df[source].ewm(span=fast, adjust=False).mean()
    slow_ema = df[source].ewm(span=slow, adjust=False).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return pd.DataFrame(
        {"macd": macd_line, "macd_signal": signal_line, "macd_hist": hist}
    )


# --------------------------------------------------------------------------- #
# "3-min strategy" components  (STRUCTURED STUBS — fill from journal rules)
# --------------------------------------------------------------------------- #
# The 3-min strategy = three composable sub-signals, remapped to 3-min bars:
#   (a) EMA mean-reversion       — fade stretch away from a fast EMA
#   (b) Bollinger VRL recovery   — "violent rejection / recovery" breakout after
#                                   a squeeze: band re-expansion + close back
#                                   through a band edge
#   (c) SMA pullback continuation— trend-following entry on a pullback to a
#                                   reference SMA in the trend direction
# Each returns a Series in {-1 short, 0 none, +1 long}. The exact thresholds are
# the trader's edge and come from Phase-2 journal extraction; the defaults below
# are placeholders so the pipeline runs end-to-end.

def ema_mean_reversion(
    df: pd.DataFrame, fast_period: int = 9, stretch_pct: float = 0.004
) -> pd.Series:
    """Fade price when it is stretched ``stretch_pct`` away from the fast EMA.

    Placeholder logic: stretched far above -> short (+expect reversion down),
    stretched far below -> long. TODO: replace with journal-calibrated bands.
    """
    fast = ema(df, fast_period)
    stretch = (df["close"] - fast) / fast
    sig = pd.Series(0, index=df.index, dtype="int8")
    sig[stretch <= -stretch_pct] = 1
    sig[stretch >= stretch_pct] = -1
    return sig.rename("sig_ema_meanrev")


def bollinger_vrl_breakout(
    df: pd.DataFrame, period: int = 20, num_std: float = 2.0
) -> pd.Series:
    """Bollinger "VRL recovery breakout": close re-entering the band after a
    poke outside it, on expanding width.

    Placeholder logic: prior bar closed below lower band, current bar closes
    back above it (bullish recovery) -> long; mirror for short. TODO: add the
    squeeze/expansion width gate and the journal's exact recovery definition.
    """
    bb = bollinger_bands(df, period=period, num_std=num_std)
    prev_below = df["close"].shift(1) < bb["bb_lower"].shift(1)
    prev_above = df["close"].shift(1) > bb["bb_upper"].shift(1)
    recover_up = prev_below & (df["close"] > bb["bb_lower"])
    recover_dn = prev_above & (df["close"] < bb["bb_upper"])
    sig = pd.Series(0, index=df.index, dtype="int8")
    sig[recover_up] = 1
    sig[recover_dn] = -1
    return sig.rename("sig_bb_vrl")


def sma_pullback_continuation(
    df: pd.DataFrame, ref_period: int = 50, trend_period: int = 200
) -> pd.Series:
    """Trend-continuation entry on a pullback to a reference SMA.

    Placeholder logic: uptrend (ref SMA above trend SMA) + price pulls back to
    touch/cross below ref SMA then closes above it -> long; mirror for short.
    TODO: replace touch test with the journal's pullback-depth + trigger rule.
    """
    ref = sma(df, ref_period)
    trend = sma(df, trend_period)
    uptrend = ref > trend
    downtrend = ref < trend
    touched_up = (df["low"] <= ref) & (df["close"] > ref)
    touched_dn = (df["high"] >= ref) & (df["close"] < ref)
    sig = pd.Series(0, index=df.index, dtype="int8")
    sig[uptrend & touched_up] = 1
    sig[downtrend & touched_dn] = -1
    return sig.rename("sig_sma_pullback")


# --------------------------------------------------------------------------- #
# One-shot: compute the full chart-layer feature set
# --------------------------------------------------------------------------- #
def compute_indicators(
    df: pd.DataFrame, params: dict | None = None
) -> pd.DataFrame:
    """Compute the full chart-layer indicator set and return a NEW frame.

    The returned frame is the input plus indicator/signal columns. ``params``
    overrides default periods; keys mirror the function arguments, e.g.::

        {"ema_fast": 9, "ema_slow": 21, "rsi_period": 14,
         "bb_period": 20, "bb_std": 2.0,
         "macd": {"fast": 12, "slow": 26, "signal": 9}}

    This is the single entry point the scoring layer calls per instrument, so
    every market gets identical feature engineering.
    """
    _require_columns(df, OHLCV_COLUMNS)
    p = params or {}
    out = df.copy()

    out[f"ema_{p.get('ema_fast', 9)}"] = ema(df, p.get("ema_fast", 9))
    out[f"ema_{p.get('ema_slow', 21)}"] = ema(df, p.get("ema_slow", 21))
    out["sma_50"] = sma(df, p.get("sma_ref", 50))
    out["sma_200"] = sma(df, p.get("sma_trend", 200))

    out = out.join(
        bollinger_bands(df, p.get("bb_period", 20), p.get("bb_std", 2.0))
    )
    out[f"rsi_{p.get('rsi_period', 14)}"] = rsi(df, p.get("rsi_period", 14))
    macd_kw = p.get("macd", {})
    out = out.join(macd(df, **macd_kw))

    # 3-min strategy component signals
    out["sig_ema_meanrev"] = ema_mean_reversion(df)
    out["sig_bb_vrl"] = bollinger_vrl_breakout(df)
    out["sig_sma_pullback"] = sma_pullback_continuation(df)

    return out
