"""Engine tests for the trader's real indicator stack: Supertrend, CPR, and the
EMA 5/45/100/200 + SMA 20 column set produced by compute_indicators.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from indicators.engine import (
    atr, supertrend, cpr, compute_indicators,
    ema5_trigger, bollinger_vrl_breakout, vwap, opening_range,
)


def _intraday(values, day="2026-06-16") -> pd.DataFrame:
    """A single-session 3-min OHLCV frame whose close follows ``values``."""
    idx = pd.date_range(f"{day} 09:15", periods=len(values), freq="3min")
    close = np.asarray(values, dtype=float)
    return pd.DataFrame(
        {"open": close, "high": close + 1.0, "low": close - 1.0, "close": close,
         "volume": np.full(len(close), 100.0)},
        index=idx,
    )


def test_vwap_within_session_range_and_zero_volume_fallback():
    df = _intraday(100 + np.cumsum(np.random.default_rng(7).standard_normal(40)))
    v = vwap(df)
    assert len(v) == len(df) and not v.isna().any()
    assert (v >= df["low"].min() - 1e-6).all() and (v <= df["high"].max() + 1e-6).all()
    # zero-volume fallback: VWAP degrades to the cumulative typical-price mean, never NaN.
    df0 = df.assign(volume=0.0)
    v0 = vwap(df0)
    assert not v0.isna().any()
    tp = (df0["high"] + df0["low"] + df0["close"]) / 3.0
    assert abs(v0.iloc[-1] - tp.mean()) < 1e-6


def test_opening_range_no_lookahead():
    # 8 bars × 3min; the first 15 minutes = the first 5 bars (09:15..09:27).
    df = _intraday([10, 12, 9, 11, 13, 20, 8, 15])
    orng = opening_range(df, minutes=15)
    # Inside/at the opening window the level is hidden (no lookahead).
    assert orng["or_high"].iloc[:5].isna().all()
    # After the window it is the first-15-min high/low, constant for the rest.
    assert (orng["or_high"].iloc[5:] == 13.0 + 1.0).all()   # high = close+1 over bars 0..4
    assert (orng["or_low"].iloc[5:] == 9.0 - 1.0).all()


def _ramp(values) -> pd.DataFrame:
    """OHLCV frame whose close follows ``values`` (high/low a hair around it)."""
    idx = pd.date_range("2024-01-01", periods=len(values), freq="1D", tz="UTC")
    close = np.asarray(values, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(len(close), 1000.0),
        },
        index=idx,
    )


def test_atr_positive_and_warmed():
    df = _ramp(100 + np.cumsum(np.random.default_rng(0).standard_normal(50)))
    a = atr(df, period=10)
    assert len(a) == len(df)
    assert (a.dropna() >= 0).all()


def test_supertrend_direction_flips_on_trend_reversal():
    # Strong up-ramp then strong down-ramp -> direction must be +1 then -1.
    up = np.linspace(100, 200, 60)
    down = np.linspace(200, 100, 60)
    df = _ramp(np.concatenate([up, down]))
    st = supertrend(df, period=10, multiplier=3.0)

    assert set(st.columns) == {"supertrend", "st_dir"}
    assert set(st["st_dir"].unique()) <= {-1, 1}
    # Late in the up-leg we are uptrend; late in the down-leg, downtrend.
    assert st["st_dir"].iloc[55] == 1
    assert st["st_dir"].iloc[-1] == -1
    # The trailing line sits below close in an uptrend, above it in a downtrend.
    assert st["supertrend"].iloc[55] <= df["close"].iloc[55]
    assert st["supertrend"].iloc[-1] >= df["close"].iloc[-1]


def test_cpr_ordering_and_prior_bar_source():
    df = _ramp([10, 20, 30, 40])
    c = cpr(df)
    assert list(c.columns) == ["cpr_pivot", "cpr_tc", "cpr_bc", "cpr_r1", "cpr_s1", "cpr_width"]
    # First row has no prior bar -> NaN; the rest are ordered bc <= pivot <= tc.
    assert c.iloc[0].isna().all()
    body = c.iloc[1:]
    assert (body["cpr_bc"] <= body["cpr_pivot"] + 1e-9).all()
    assert (body["cpr_pivot"] <= body["cpr_tc"] + 1e-9).all()
    # Row 1's pivot is built from row 0's H/L/C (10.5, 9.5, 10) -> 10.0.
    assert c["cpr_pivot"].iloc[1] == np.float64(10.0)
    # cpr_width is the normalised range (tc-bc)/pivot, non-negative once warmed.
    assert (body["cpr_width"] >= -1e-9).all()


def test_compute_indicators_emits_full_stack():
    df = _ramp(100 + np.cumsum(np.random.default_rng(1).standard_normal(260)))
    feats = compute_indicators(df)
    for col in (
        "ema_5", "ema_45", "ema_100", "ema_200", "sma_20",
        "bb_pctb", "rsi_14", "macd_hist",
        "supertrend", "st_dir", "cpr_pivot", "cpr_tc", "cpr_bc",
        "sig_ema5_trigger", "sig_bb_vrl", "sig_sma_pullback",
    ):
        assert col in feats.columns, f"missing {col}"
    # Past the 200-bar warm-up the long EMA is populated.
    assert feats["ema_200"].iloc[-1] == feats["ema_200"].iloc[-1]  # not NaN


def test_ema5_trigger_close_vs_ema5():
    df = _ramp(np.linspace(100, 200, 80))
    sig = ema5_trigger(df)
    assert set(sig.unique()) <= {-1, 0, 1}
    # On a pure up-ramp, close stays above the 5-EMA -> +1 late.
    assert sig.iloc[-1] == 1


def test_bollinger_vrl_requires_squeeze():
    # Long flat (squeeze) section then a downward poke + recovery. The gated
    # signal must be a subset of {-1,0,1} and only fire on re-expansion.
    rng = np.random.default_rng(4)
    flat = np.full(80, 100.0) + rng.standard_normal(80) * 0.01  # crushed width
    move = np.array([100.0, 98.0, 101.0])                       # poke down + recover
    df = _ramp(np.concatenate([flat, move]))
    sig = bollinger_vrl_breakout(df)
    assert set(sig.unique()) <= {-1, 0, 1}
    # Inside the dead-flat squeeze region (no expansion yet) nothing fires.
    assert (sig.iloc[5:75] == 0).all()


def test_compute_indicators_honours_param_overrides():
    df = _ramp(100 + np.cumsum(np.random.default_rng(2).standard_normal(80)))
    feats = compute_indicators(
        df, {"ema_periods": [8, 21], "sma_period": 10,
             "supertrend": {"period": 7, "multiplier": 2.0}}
    )
    assert "ema_8" in feats.columns and "ema_21" in feats.columns
    assert "sma_10" in feats.columns
    assert "ema_5" not in feats.columns  # default set was overridden
