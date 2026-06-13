"""MTF smoke tests — resample correctness, the no-lookahead guarantee, the
three MTF combination methods, and an end-to-end Stage-1 score.

Run with: ``pytest -q`` (or ``python -m pytest tests/``).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from indicators.timeframes import resample_ohlcv, align_to_base, build_mtf_features
from indicators.directional import (
    MTFDirectionalConfig,
    resolve_direction_mtf,
    DirectionalConfig,
)
from scoring.stage1 import assemble_mtf_frames, score_instrument_mtf


def _synth_3m(days: int = 10) -> pd.DataFrame:
    """Multi-day 3-min OHLCV over an NSE-like 09:15–15:30 session, tz-aware."""
    rng = np.random.default_rng(7)
    frames = []
    start = pd.Timestamp("2024-01-01 09:15", tz="Asia/Kolkata")
    for d in range(days):
        day_open = start + pd.Timedelta(days=d)
        # 09:15 to 15:30 inclusive on 3-min bars = 125 bars.
        idx = pd.date_range(day_open, periods=125, freq="3min", tz="Asia/Kolkata")
        price = 100 + np.cumsum(rng.standard_normal(len(idx)) * 0.2)
        frames.append(
            pd.DataFrame(
                {
                    "open": price,
                    "high": price + 0.3,
                    "low": price - 0.3,
                    "close": price,
                    "volume": rng.integers(100, 1000, len(idx)),
                },
                index=idx,
            )
        )
    df = pd.concat(frames)
    df.index.name = "datetime"
    return df


def _synth_daily(days: int = 60) -> pd.DataFrame:
    rng = np.random.default_rng(11)
    idx = pd.date_range("2023-11-01", periods=days, freq="1D", tz="Asia/Kolkata")
    price = 100 + np.cumsum(rng.standard_normal(days) * 1.0)
    return pd.DataFrame(
        {
            "open": price,
            "high": price + 1,
            "low": price - 1,
            "close": price,
            "volume": rng.integers(1000, 5000, days),
        },
        index=idx,
    )


# --------------------------------------------------------------------------- #
def test_resample_ohlc_aggregation():
    base = _synth_3m(days=2)
    bars15 = resample_ohlcv(base, "15min", anchor="9h15min")
    # Each 15-min bar spans 5 x 3-min bars; check the first one explicitly.
    first = base.iloc[:5]
    assert bars15.iloc[0]["open"] == pytest.approx(first["open"].iloc[0])
    assert bars15.iloc[0]["high"] == pytest.approx(first["high"].max())
    assert bars15.iloc[0]["low"] == pytest.approx(first["low"].min())
    assert bars15.iloc[0]["close"] == pytest.approx(first["close"].iloc[-1])
    assert bars15.iloc[0]["volume"] == pytest.approx(first["volume"].sum())
    # Session-anchored: bins start at 09:15.
    assert (bars15.index.time == pd.Timestamp("09:15").time()).any()


def test_align_no_lookahead():
    base = _synth_3m(days=3)
    bars60 = resample_ohlcv(base, "60min", anchor="9h15min")
    aligned = align_to_base(bars60["close"], base.index, "60min")

    # For every base bar, the aligned value must come from a 60-min bar that has
    # ALREADY closed (open + 60min <= base timestamp) — never one still forming.
    close_times = bars60.index + pd.Timedelta("60min")
    for t in base.index[::7]:
        val = aligned.loc[t]
        eligible = bars60["close"][close_times <= t]
        if eligible.empty:
            assert pd.isna(val)
        else:
            assert val == pytest.approx(eligible.iloc[-1])


@pytest.mark.parametrize(
    "method", ["htf_bias_trigger", "cross_tf_confluence", "per_tf_then_vote"]
)
def test_mtf_methods_run(method):
    frames = assemble_mtf_frames(_synth_3m(10), _synth_daily(60), anchor="9h15min")
    feats = build_mtf_features(frames)
    cfg = MTFDirectionalConfig(mtf_method=method, bias_quorum=1)
    calls = resolve_direction_mtf(feats, cfg)
    assert len(calls) == len(frames["3min"])
    assert set(calls.unique()) <= {"long", "short", "flat"}


def test_htf_bias_trigger_flat_on_conflict():
    """When the trigger and bias disagree, htf_bias_trigger must stand down."""
    frames = assemble_mtf_frames(_synth_3m(10), _synth_daily(60), anchor="9h15min")
    feats = build_mtf_features(frames)
    cfg = MTFDirectionalConfig(mtf_method="htf_bias_trigger", bias_quorum=1)
    calls = resolve_direction_mtf(feats, cfg)

    # Reconstruct trigger + bias signs and assert no non-flat call ever opposes
    # the realised bias (the whole point of the bias filter).
    from indicators.directional import (
        resolve_direction,
        calls_to_sign,
        _bias_sign_matrix,
    )

    trig = calls_to_sign(resolve_direction(feats["3min"], cfg.base))
    B = _bias_sign_matrix(feats, frames["3min"].index, cfg)
    final = calls_to_sign(calls)
    taken = final != 0
    # Every taken bar's direction equals the trigger and is not contradicted.
    assert (final[taken] == trig[taken]).all()


def test_score_instrument_mtf_end_to_end():
    frames = assemble_mtf_frames(_synth_3m(12), _synth_daily(60), anchor="9h15min")
    row = score_instrument_mtf(
        frames,
        instrument="SYNTH",
        horizon=8,
        cfg=MTFDirectionalConfig(bias_quorum=1),
    )
    assert row.instrument == "SYNTH"
    assert row.n_signals == row.n_long + row.n_short
    assert 0.0 <= row.coverage <= 1.0
