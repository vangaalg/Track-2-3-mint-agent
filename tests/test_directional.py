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
    from indicators.directional import resolve_direction_mtf, MTFDirectionalConfig
    up = compute_indicators(_frame(np.linspace(100, 300, 300)))
    down = compute_indicators(_frame(np.linspace(300, 100, 300)))
    feats = {"3min": up, "15min": down, "60min": down, "1day": down, "1week": down}
    # a net-sign three_min base (no confirm gate) DOES fire long on a clean up-ramp.
    base = DirectionalConfig(voters=["three_min"], min_agree=1)
    cfg = MTFDirectionalConfig(base=base, mtf_method="trigger_only")
    calls = resolve_direction_mtf(feats, cfg)
    solo = resolve_direction(up, base)
    assert (calls == solo).all() and (calls == "long").any()   # not gated by the HTF
    # the same setup under htf_bias_trigger IS suppressed by the conflicting HTF stack
    gated = resolve_direction_mtf(feats, MTFDirectionalConfig(base=base,
                                                              mtf_method="htf_bias_trigger"))
    assert (gated == "flat").all()


# --- event-gated Bollinger-reversal trigger (the journal's real 3-min entry) ---- #
def _trig_frame(ema5, bbv, n=None):
    """Frame carrying just the two sig columns the bb_reversal voter reads."""
    n = n or len(ema5)
    idx = pd.date_range("2024-01-01 09:15", periods=n, freq="3min", tz="Asia/Kolkata")
    return pd.DataFrame({"sig_ema5_trigger": ema5, "sig_bb_vrl": bbv}, index=idx)


def test_bb_reversal_event_arms_holds_and_exits():
    from indicators.directional import vote_bb_reversal
    # bar2: bb event (+1) with EMA-5 agreeing (+1) -> arm long; hold while EMA-5 +1;
    # bar5: EMA-5 flips to -1 -> exit to flat; no re-arm without a fresh event.
    ema5 = [1,  1,  1,  1,  1, -1, -1,  1]
    bbv  = [0,  0,  1,  0,  0,  0,  0,  0]
    v = vote_bb_reversal(_trig_frame(ema5, bbv)).tolist()
    assert v == [0, 0, 1, 1, 1, 0, 0, 0]


def test_bb_reversal_ema5_alone_never_arms():
    from indicators.directional import vote_bb_reversal
    # price sits above the EMA-5 the whole time but NO Bollinger event -> never fires.
    ema5 = [1] * 8
    bbv = [0] * 8
    assert vote_bb_reversal(_trig_frame(ema5, bbv)).abs().sum() == 0


def test_bb_reversal_event_must_agree_with_ema5():
    from indicators.directional import vote_bb_reversal
    # bb event says long (+1) but the close is still below the EMA-5 (-1) -> no arm.
    ema5 = [-1, -1, -1, -1]
    bbv = [0, 1, 0, 0]
    assert vote_bb_reversal(_trig_frame(ema5, bbv)).abs().sum() == 0


def test_squeeze_config_fires_one_trigger_per_reversal():
    from indicators.directional import resolve_direction, squeeze_trigger_config
    # The SEPARATE squeeze fade: a held long reversal with EXPANDING volume ->
    # confirm_2_close passes from the 2nd held bar; one trigger, not bar-by-bar chop.
    n = 8
    idx = pd.date_range("2024-01-01 09:15", periods=n, freq="3min", tz="Asia/Kolkata")
    df = pd.DataFrame({
        "sig_ema5_trigger": [1, 1, 1, 1, 1, -1, -1, -1],
        "sig_bb_vrl":       [0, 0, 1, 0, 0, 0, 0, 0],
        "volume": np.arange(1, n + 1, dtype=float) * 1000,   # strictly expanding
    }, index=idx)
    calls = resolve_direction(df, squeeze_trigger_config()).tolist()
    longs = [i for i, c in enumerate(calls) if c == "long"]
    assert longs and all(c in ("long", "flat") for c in calls)   # never flips to short
    flips = sum(1 for i in range(1, n) if calls[i] == "long" and calls[i-1] != "long")
    assert flips == 1 and longs[0] >= 3


# --- breakout -> VRL retest + 5-EMA close (the trader's real 3-min entry) ------- #
def _bp_frame(rows):
    """rows: (close, low, high, bb_upper, bb_lower, ema_5, ema_45) per bar."""
    idx = pd.date_range("2024-01-01 09:15", periods=len(rows), freq="3min", tz="Asia/Kolkata")
    cols = ["close", "low", "high", "bb_upper", "bb_lower", "ema_5", "ema_45"]
    return pd.DataFrame(rows, columns=cols, index=idx)


def test_breakout_vrl_retest_fires_long_on_close_below_5ema():
    from indicators.directional import vote_breakout_pullback
    # bar0: up-breach above the 45-EMA -> arm long, VRL = breach high (112).
    # bar1: retest low<=VRL, close>VRL, and CLOSES BELOW the 5-EMA -> FIRE long.
    df = _bp_frame([
        (110.0, 108.0, 112.0, 109.0, 90.0, 105.0, 100.0),   # arm, VRL=112
        (113.0, 111.0, 117.0, 109.0, 90.0, 120.0, 100.0),   # low<=112, close>112, close<5EMA -> FIRE
    ])
    assert vote_breakout_pullback(df).tolist() == [0, 1]


def test_breakout_vrl_retest_above_5ema_does_not_fire():
    from indicators.directional import vote_breakout_pullback
    # the 14:03 case: retest touches the VRL and holds, but CLOSES ABOVE the 5-EMA -> no entry.
    df = _bp_frame([
        (110.0, 108.0, 112.0, 109.0, 90.0, 105.0, 100.0),   # arm, VRL=112
        (113.0, 111.0, 117.0, 109.0, 90.0, 112.5, 100.0),   # low<=112, close>112 but close>5EMA
    ])
    assert vote_breakout_pullback(df).abs().sum() == 0


def test_breakout_vrl_breakdown_does_not_fire():
    from indicators.directional import vote_breakout_pullback
    # close back BELOW the VRL (a breakdown through the breakout origin) is not an entry.
    df = _bp_frame([
        (110.0, 108.0, 112.0, 109.0, 90.0, 105.0, 100.0),   # arm, VRL=112
        (111.5, 110.0, 113.0, 109.0, 90.0, 120.0, 100.0),   # low<=112 but close 111.5 < VRL 112
    ])
    assert vote_breakout_pullback(df).abs().sum() == 0


def test_breakout_vrl_retest_short_mirror():
    from indicators.directional import vote_breakout_pullback
    # bar0: down-breach below the 45-EMA -> arm short, VRL = breach low (88).
    # bar1: retest high>=VRL, close<VRL, and CLOSES ABOVE the 5-EMA -> FIRE short.
    df = _bp_frame([
        (90.0, 88.0, 92.0, 110.0, 91.0, 95.0, 100.0),       # arm, VRL=88
        (87.0, 86.0, 89.0, 110.0, 91.0, 85.0, 100.0),       # high>=88, close<88, close>5EMA -> FIRE
    ])
    assert vote_breakout_pullback(df).tolist() == [0, -1]


def test_breakout_vrl_cancels_on_45ema_break():
    from indicators.directional import vote_breakout_pullback
    df = _bp_frame([
        (110.0, 108.0, 112.0, 109.0, 90.0, 105.0, 100.0),   # arm long, VRL=112
        (95.0, 94.0, 96.0, 120.0, 90.0, 105.0, 100.0),      # close<45EMA -> CANCEL (no re-arm: close<upper)
        (113.0, 111.0, 117.0, 120.0, 90.0, 120.0, 100.0),   # would retest, but disarmed -> no fire
    ])
    assert vote_breakout_pullback(df).abs().sum() == 0


def test_breakout_vrl_one_fire_per_setup():
    from indicators.directional import vote_breakout_pullback
    df = _bp_frame([
        (110.0, 108.0, 112.0, 109.0, 90.0, 105.0, 100.0),   # arm, VRL=112
        (113.0, 111.0, 117.0, 109.0, 90.0, 120.0, 100.0),   # FIRE long
        (113.0, 111.0, 114.0, 120.0, 90.0, 120.0, 100.0),   # retest-like but flat & no new breach
    ])
    assert vote_breakout_pullback(df).tolist() == [0, 1, 0]


# --- MTF 45-EMA confidence: multi-timeframe agreement grades conviction --------- #
def _conf_frame(close, ema45):
    idx = pd.date_range("2024-01-01", periods=len(close), freq="1D", tz="UTC")
    df = pd.DataFrame({"close": np.asarray(close, float)}, index=idx)
    df["ema_45"] = float(ema45)
    return df


def test_mtf_ema45_confidence_counts_agreement():
    from indicators.directional import mtf_ema45_confidence
    n = 12   # > 1 week so the weekly alignment lands a (no-lookahead) match
    idx = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    price = [100.0] * n
    base = _conf_frame(price, 90.0)          # carries "close" for the helper
    # All five HTF 45-EMAs BELOW price -> every TF supports a long.
    feats = {"3min": base,
             "15min": _conf_frame(price, 90), "30min": _conf_frame(price, 91),
             "60min": _conf_frame(price, 92), "1day": _conf_frame(price, 93),
             "1week": _conf_frame(price, 94)}
    longs = pd.Series(["long"] * n, index=idx)
    conf, align = mtf_ema45_confidence(feats, longs)
    assert int(conf.iloc[-1]) == 5
    assert set(align.columns) == {"15min", "30min", "60min", "1day", "1week"}

    # Same EMAs (all below price) but the SIGNAL is short -> none support it.
    shorts = pd.Series(["short"] * n, index=idx)
    conf_s, _ = mtf_ema45_confidence(feats, shorts)
    assert int(conf_s.iloc[-1]) == 0

    # Flat call -> confidence 0 regardless of alignment.
    flat = pd.Series(["flat"] * n, index=idx)
    conf_f, _ = mtf_ema45_confidence(feats, flat)
    assert int(conf_f.iloc[-1]) == 0


def test_mtf_ema45_confidence_partial_and_missing_tfs():
    from indicators.directional import mtf_ema45_confidence
    n = 4
    idx = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    price = [100.0] * n
    # 15m + 30m below price (support long), 60m above (opposes); daily/weekly absent.
    feats = {"3min": _conf_frame(price, 90),
             "15min": _conf_frame(price, 95), "30min": _conf_frame(price, 96),
             "60min": _conf_frame(price, 105)}
    conf, align = mtf_ema45_confidence(feats, pd.Series(["long"] * n, index=idx))
    assert int(conf.iloc[-1]) == 2
    assert "1day" not in align.columns         # missing TFs simply don't count

    # No HTF feats at all (3min only) -> confidence 0, no crash.
    conf0, _ = mtf_ema45_confidence({"3min": _conf_frame(price, 90)},
                                    pd.Series(["long"] * n, index=idx))
    assert int(conf0.iloc[-1]) == 0
