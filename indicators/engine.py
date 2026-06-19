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
# ATR / Supertrend
# --------------------------------------------------------------------------- #
def atr(df: pd.DataFrame, period: int = 10) -> pd.Series:
    """Average True Range (Wilder smoothing).

    TR = max(high-low, |high-prev_close|, |low-prev_close|); ATR is the Wilder
    EMA (``alpha = 1/period``) of TR. Used by Supertrend.
    """
    _require_columns(df, ["high", "low", "close"])
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            (df["high"] - df["low"]).abs(),
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean().rename(f"atr_{period}")


def supertrend(
    df: pd.DataFrame, period: int = 7, multiplier: float = 3.0
) -> pd.DataFrame:
    """Supertrend trailing line + direction.

    Returns columns ``supertrend`` (the trailing stop line) and ``st_dir``
    (+1 uptrend / -1 downtrend). Standard ATR-band algorithm with the
    carry-forward final-band rule; computed with an explicit loop, which is fine
    for the few-thousand-bar frames Stage 1 scores.
    """
    _require_columns(df, ["high", "low", "close"])
    a = atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2.0
    upper_basic = (hl2 + multiplier * a).to_numpy()
    lower_basic = (hl2 - multiplier * a).to_numpy()
    close = df["close"].to_numpy()
    n = len(df)

    final_upper = [0.0] * n
    final_lower = [0.0] * n
    st = [float("nan")] * n
    direction = [1] * n

    for i in range(n):
        if i == 0:
            final_upper[i] = upper_basic[i]
            final_lower[i] = lower_basic[i]
            direction[i] = 1
            st[i] = lower_basic[i]
            continue
        final_upper[i] = (
            upper_basic[i]
            if (upper_basic[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1])
            else final_upper[i - 1]
        )
        final_lower[i] = (
            lower_basic[i]
            if (lower_basic[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1])
            else final_lower[i - 1]
        )
        if close[i] > final_upper[i - 1]:
            direction[i] = 1
        elif close[i] < final_lower[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]
        st[i] = final_lower[i] if direction[i] == 1 else final_upper[i]

    return pd.DataFrame(
        {"supertrend": st, "st_dir": direction}, index=df.index
    ).astype({"st_dir": "int8"})


# --------------------------------------------------------------------------- #
# CPR — Central Pivot Range (from the PRIOR period's H/L/C)
# --------------------------------------------------------------------------- #
def cpr(df: pd.DataFrame) -> pd.DataFrame:
    """Central Pivot Range — the classic **daily** CPR, broadcast onto every bar.

    CPR is defined off the daily session regardless of the chart timeframe: each
    session's levels come from the PRIOR session's H/L/C and are constant for the
    whole day — exactly how a charting platform overlays CPR on a 3-min chart.

    Implementation: group bars by trading session (``index.normalize()``),
    aggregate per-session H/L/C, take the prior session (``shift(1)`` →
    **no lookahead**: today's CPR uses yesterday's completed session, known at the
    open; today's still-forming session is never used), then broadcast each
    session's levels back onto its bars. On a daily frame each bar is its own
    session, so this reduces to "prior bar's HLC".

    Columns: ``cpr_pivot``, ``cpr_tc`` (top central), ``cpr_bc`` (bottom central),
    ``cpr_r1``, ``cpr_s1``.
    """
    _require_columns(df, ["high", "low", "close"])
    sess = df.index.normalize()
    g = df.groupby(sess)
    daily = pd.DataFrame(
        {"high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last()}
    )
    prior = daily.shift(1)

    pivot = (prior["high"] + prior["low"] + prior["close"]) / 3.0
    bc = (prior["high"] + prior["low"]) / 2.0
    tc = 2.0 * pivot - bc
    # TC/BC are orientation-free — order them so cpr_bc <= cpr_pivot <= cpr_tc.
    top = pd.concat([tc, bc], axis=1).max(axis=1)
    bot = pd.concat([tc, bc], axis=1).min(axis=1)
    levels = pd.DataFrame(
        {
            "cpr_pivot": pivot,
            "cpr_tc": top,
            "cpr_bc": bot,
            "cpr_r1": 2.0 * pivot - prior["low"],
            "cpr_s1": 2.0 * pivot - prior["high"],
        }
    )
    # Broadcast each session's levels back onto all of that session's bars.
    return levels.reindex(sess).set_axis(df.index)


# --------------------------------------------------------------------------- #
# "3-min strategy" components  (STRUCTURED STUBS — fill from journal rules)
# --------------------------------------------------------------------------- #
# The "3-min strategy" = the journal-faithful trio the trader actually trades
# (see indicators/JOURNAL_EXTRACTION.md):
#   (a) 3m-EMA5 trigger          — price holding above/below the fast (5) EMA
#                                   ("price holds above/below the 3-min EMA")
#   (b) Bollinger VRL recovery   — recovery back through a band edge AFTER a
#                                   squeeze ("Bollinger crushed → re-expands")
#   (c) 45-EMA pullback contin.  — pullback to the 45-EMA in the regime direction
#                                   ("sell strength into the falling 45-EMA;
#                                    buy the pullback in trend")
# Each returns a Series in {-1 short, 0 none, +1 long}. Numeric defaults are
# PROVISIONAL (journal's own status) — registered in JOURNAL_EXTRACTION.md.
# NOTE: ``ema_mean_reversion`` below is kept for experimentation but is NOT part
# of the journal trio — the trader is a trend-follower, not a mean-reverter — so
# it is excluded from ``vote_three_min``.

def ema5_trigger(df: pd.DataFrame, period: int = 5) -> pd.Series:
    """3-min entry trigger: close holding above/below the fast (5) EMA.

    Journal master trigger ("price holds above/below the 3-min EMA-5"). +1 when
    close is above the EMA-5, -1 below. Confirmation (2-close + volume) is a
    separate gate applied at the resolver layer (see directional.confirm_2_close).
    """
    fast = ema(df, period)
    sig = pd.Series(0, index=df.index, dtype="int8")
    sig[df["close"] > fast] = 1
    sig[df["close"] < fast] = -1
    return sig.rename("sig_ema5_trigger")


def ema_mean_reversion(
    df: pd.DataFrame, fast_period: int = 5, stretch_pct: float = 0.004
) -> pd.Series:
    """Fade price when it is stretched ``stretch_pct`` away from the fast EMA.

    NOT the journal's style (the trader trends, he does not fade) — retained for
    completeness/experimentation and EXCLUDED from ``vote_three_min``. Stretched
    far above -> short, far below -> long.
    """
    fast = ema(df, fast_period)
    stretch = (df["close"] - fast) / fast
    sig = pd.Series(0, index=df.index, dtype="int8")
    sig[stretch <= -stretch_pct] = 1
    sig[stretch >= stretch_pct] = -1
    return sig.rename("sig_ema_meanrev")


def bollinger_vrl_breakout(
    df: pd.DataFrame,
    period: int = 20,
    num_std: float = 2.0,
    squeeze_window: int = 50,
    squeeze_pct: float = 0.25,
) -> pd.Series:
    """Bollinger "VRL recovery breakout": close re-entering the band after a
    poke outside it, gated by a prior **squeeze** that is now re-expanding.

    Journal read ("Bollinger crushed → re-expands"): a recovery only counts when
    the PRIOR bar's band width sat in the low ``squeeze_pct`` quantile of its
    trailing ``squeeze_window`` (the coil) AND width is now expanding. Prior bar
    closed below the lower band, current closes back above it -> long; mirror for
    short. ``squeeze_window``/``squeeze_pct`` PROVISIONAL (see JOURNAL_EXTRACTION).
    """
    bb = bollinger_bands(df, period=period, num_std=num_std)
    width = bb["bb_width"]
    sq_thresh = width.rolling(squeeze_window).quantile(squeeze_pct)
    was_squeezed = width.shift(1) <= sq_thresh.shift(1)
    expanding = width > width.shift(1)
    gate = was_squeezed & expanding

    prev_below = df["close"].shift(1) < bb["bb_lower"].shift(1)
    prev_above = df["close"].shift(1) > bb["bb_upper"].shift(1)
    recover_up = prev_below & (df["close"] > bb["bb_lower"])
    recover_dn = prev_above & (df["close"] < bb["bb_upper"])
    sig = pd.Series(0, index=df.index, dtype="int8")
    sig[recover_up & gate] = 1
    sig[recover_dn & gate] = -1
    return sig.rename("sig_bb_vrl")


def sma_pullback_continuation(
    df: pd.DataFrame, regime_period: int = 45, trend_period: int = 200
) -> pd.Series:
    """Trend-continuation pullback to the **45-EMA** (the journal's master MA).

    Journal read ("buy the pullback in an up-regime; sell strength into the
    falling 45-EMA"): regime = 45-EMA vs 200-EMA (up if 45 above 200). In an
    up-regime a bar that tags/dips the 45-EMA and closes back above it -> long;
    mirror for short. (Column name kept ``sig_sma_pullback`` for continuity.)
    """
    regime = ema(df, regime_period)
    trend = ema(df, trend_period)
    uptrend = regime > trend
    downtrend = regime < trend
    touched_up = (df["low"] <= regime) & (df["close"] > regime)
    touched_dn = (df["high"] >= regime) & (df["close"] < regime)
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

        {"ema_periods": [5, 45, 100, 200], "sma_period": 20, "rsi_period": 14,
         "bb_period": 20, "bb_std": 2.0,
         "supertrend": {"period": 10, "multiplier": 3.0},
         "macd": {"fast": 12, "slow": 26, "signal": 9}}

    This is the trader's real chart stack: EMA 5/45/100/200, SMA 20, Bollinger,
    RSI, MACD, Supertrend, and CPR pivots (the last meaningful on daily/weekly).
    Single entry point the scoring layer calls per instrument, so every market
    gets identical feature engineering.
    """
    _require_columns(df, OHLCV_COLUMNS)
    p = params or {}
    out = df.copy()

    for period in p.get("ema_periods", [5, 45, 100, 200]):
        out[f"ema_{period}"] = ema(df, period)
    sma_period = p.get("sma_period", 20)
    out[f"sma_{sma_period}"] = sma(df, sma_period)

    out = out.join(
        bollinger_bands(df, p.get("bb_period", 20), p.get("bb_std", 2.0))
    )
    out[f"rsi_{p.get('rsi_period', 14)}"] = rsi(df, p.get("rsi_period", 14))
    out = out.join(macd(df, **p.get("macd", {})))
    out = out.join(supertrend(df, **p.get("supertrend", {})))
    out = out.join(cpr(df))

    # 3-min strategy component signals (journal trio + the kept meanrev experiment)
    out["sig_ema5_trigger"] = ema5_trigger(df)
    out["sig_bb_vrl"] = bollinger_vrl_breakout(df)
    out["sig_sma_pullback"] = sma_pullback_continuation(df)
    out["sig_ema_meanrev"] = ema_mean_reversion(df)  # not in the journal trio

    return out
