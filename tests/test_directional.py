"""Voter tests for the new indicators (ema_stack, supertrend, cpr) and that they
resolve through the single long/short/flat resolver.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from indicators.engine import compute_indicators
from indicators.directional import (
    vote_ema_stack,
    vote_supertrend,
    vote_cpr,
    vote_regime_45,
    vote_ema5_trigger,
    confirm_2_close,
    VOTERS,
    DirectionalConfig,
    resolve_direction,
)


def _frame(close) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(close), freq="1D", tz="UTC")
    close = np.asarray(close, dtype=float)
    return pd.DataFrame(
        {"open": close, "high": close + 0.5, "low": close - 0.5,
         "close": close, "volume": np.full(len(close), 1000.0)},
        index=idx,
    )


def test_vote_ema_stack_signs():
    # A clean up-ramp -> ribbon stacked up -> long late in the series.
    up = compute_indicators(_frame(np.linspace(100, 300, 260)))
    v = vote_ema_stack(up)
    assert set(v.unique()) <= {-1, 0, 1}
    assert v.iloc[-1] == 1

    down = compute_indicators(_frame(np.linspace(300, 100, 260)))
    assert vote_ema_stack(down).iloc[-1] == -1


def test_vote_supertrend_matches_st_dir():
    feats = compute_indicators(
        _frame(np.concatenate([np.linspace(100, 200, 60), np.linspace(200, 100, 60)]))
    )
    v = vote_supertrend(feats)
    assert (v == feats["st_dir"]).all()
    assert set(v.unique()) <= {-1, 1}


def test_vote_cpr_signs():
    feats = compute_indicators(_frame(100 + np.cumsum(
        np.random.default_rng(0).standard_normal(60))))
    v = vote_cpr(feats)
    assert set(v.unique()) <= {-1, 0, 1}
    # Sanity: a long vote means close is above the top central line.
    longs = v == 1
    assert (feats.loc[longs, "close"] > feats.loc[longs, "cpr_tc"]).all()


def test_new_voters_registered_and_resolve():
    for name in ("ema_stack", "supertrend", "cpr", "regime_45", "ema5_trigger"):
        assert name in VOTERS
    feats = compute_indicators(_frame(100 + np.cumsum(
        np.random.default_rng(1).standard_normal(260))))
    cfg = DirectionalConfig(
        voters=["ema_stack", "supertrend", "cpr", "regime_45", "ema5_trigger"],
        min_agree=2,
    )
    calls = resolve_direction(feats, cfg)
    assert set(calls.unique()) <= {"long", "short", "flat"}
    assert len(calls) == len(feats)


def test_vote_regime_45_close_vs_ema45():
    feats = compute_indicators(_frame(np.linspace(100, 300, 260)))
    v = vote_regime_45(feats)
    assert set(v.unique()) <= {-1, 0, 1}
    # On a clean up-ramp, late bars are above the 45-EMA -> long-regime.
    assert v.iloc[-1] == 1
    longs = v == 1
    assert (feats.loc[longs, "close"] > feats.loc[longs, "ema_45"]).all()


def test_vote_ema5_trigger_matches_signal_column():
    feats = compute_indicators(_frame(100 + np.cumsum(
        np.random.default_rng(2).standard_normal(120))))
    v = vote_ema5_trigger(feats)
    assert (v == feats["sig_ema5_trigger"]).all()
    assert set(v.unique()) <= {-1, 0, 1}


def test_confirm_2_close_suppresses_flip_passes_persistent():
    # Volume rising every bar so the volume gate is always satisfied.
    n = 30
    idx = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    df = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
         "volume": np.arange(1, n + 1, dtype=float) * 1000},
        index=idx,
    )
    # Alternating +1/-1 -> never two consecutive same-sign closes -> all gated.
    flip = pd.Series([1, -1] * (n // 2), index=idx, name="vote_x")
    assert (confirm_2_close(flip, df) == 0).all()

    # A persistent +1 (after the first bar) passes once it has held 2 closes.
    persistent = pd.Series([1] * n, index=idx, name="vote_x")
    gated = confirm_2_close(persistent, df)
    assert gated.iloc[0] == 0      # only one close so far
    assert (gated.iloc[1:] == 1).all()


def test_confirm_2_close_zero_volume_fallback():
    # FX/index style: volume all zero -> price-persistence only, never blanked.
    n = 10
    idx = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    df = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0.0},
        index=idx,
    )
    persistent = pd.Series([-1] * n, index=idx, name="vote_x")
    gated = confirm_2_close(persistent, df)
    assert gated.iloc[0] == 0
    assert (gated.iloc[1:] == -1).all()  # volume gate skipped, persistence holds


# --- journal 3-min strategy: confirm hook, NaN-safe trio, trigger-only MTF ----- #
def test_resolve_direction_confirm_closes_gates():
    feats = compute_indicators(_frame(np.linspace(100, 300, 260)))
    raw = DirectionalConfig(voters=["three_min"], min_agree=1)
    gated = DirectionalConfig(voters=["three_min"], min_agree=1, confirm_closes=2)
    calls_raw = resolve_direction(feats, raw)
    calls_gated = resolve_direction(feats, gated)
    # The gate can only REMOVE calls (turn some to flat), never invent new ones.
    nonflat_raw = (calls_raw != "flat").sum()
    nonflat_gated = (calls_gated != "flat").sum()
    assert nonflat_gated <= nonflat_raw
    # Every surviving gated call must match the raw call at that bar (no flips).
    survived = calls_gated != "flat"
    assert (calls_gated[survived] == calls_raw[survived]).all()


def test_vote_three_min_nan_safe():
    from indicators.directional import vote_three_min
    feats = compute_indicators(_frame(100 + np.cumsum(
        np.random.default_rng(3).standard_normal(40)))).copy()
    feats["sig_bb_vrl"] = feats["sig_bb_vrl"].astype(float)
    feats.loc[feats.index[0], "sig_bb_vrl"] = np.nan   # a warm-up NaN must not raise
    v = vote_three_min(feats)
    assert set(v.unique()) <= {-1, 0, 1} and len(v) == len(feats)


def test_trigger_only_ignores_htf_gate():
    from indicators.directional import (
        resolve_direction_mtf, journal_mtf_config, MTFDirectionalConfig)
    up = compute_indicators(_frame(np.linspace(100, 300, 300)))
    down = compute_indicators(_frame(np.linspace(300, 100, 300)))
    feats = {"3min": up, "15min": down, "60min": down, "1day": down, "1week": down}
    cfg = journal_mtf_config()
    cfg.validate()
    calls = resolve_direction_mtf(feats, cfg)
    # trigger_only == the pure 3-min read, regardless of an opposing HTF stack
    solo = resolve_direction(up, cfg.base)
    assert (calls == solo).all()
    # the same setup under htf_bias_trigger WOULD be suppressed by the conflict
    gated_cfg = MTFDirectionalConfig(base=cfg.base, mtf_method="htf_bias_trigger")
    gated = resolve_direction_mtf(feats, gated_cfg)
    assert (gated == "flat").all()
