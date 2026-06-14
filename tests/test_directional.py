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
    for name in ("ema_stack", "supertrend", "cpr"):
        assert name in VOTERS
    feats = compute_indicators(_frame(100 + np.cumsum(
        np.random.default_rng(1).standard_normal(260))))
    cfg = DirectionalConfig(
        voters=["ema_stack", "supertrend", "cpr"], min_agree=2
    )
    calls = resolve_direction(feats, cfg)
    assert set(calls.unique()) <= {"long", "short", "flat"}
    assert len(calls) == len(feats)
