"""Strike-selection agent — pick the nearest-to-money ITM strike with low theta."""

from __future__ import annotations

import numpy as np
import pandas as pd

from feeds.oi import chain_table
from analysis.strike import select_strike


def _table(spot, call_ltp_fn, put_ltp_fn, lo=23000, hi=25000, step=50):
    strikes = [float(s) for s in range(lo, hi + step, step)]
    df = pd.DataFrame({
        "strike": strikes,
        "call_oi": [1.0] * len(strikes),
        "put_oi": [1.0] * len(strikes),
        "call_ltp": [call_ltp_fn(s) for s in strikes],
        "put_ltp": [put_ltp_fn(s) for s in strikes],
    })
    return chain_table(df, spot=spot, window=1000)


def test_long_takes_nearest_itm_within_tolerance():
    spot = 24000.0
    # CE extrinsic grows the nearer to money you go; 23500 has extrinsic 10 (<=25).
    # call_ltp = intrinsic + extrinsic, extrinsic = (strike-23000)/50 * 5  -> 23500 -> 50pts? tune:
    def call_ltp(s):
        intrinsic = max(spot - s, 0)
        extrinsic = max(0, (s - 23000) / 100.0)   # 23000->0, 23500->5, 23900->9
        return intrinsic + extrinsic
    t = _table(spot, call_ltp, lambda s: max(s - spot, 0) + 50)
    pick = select_strike(t, spot, "long")
    assert pick["right"] == "CE"
    # nearest-to-money strike whose extrinsic <= 25 — that's the highest (23950) here,
    # since even 23950 extrinsic = 9.5 <= 25.
    assert pick["strike"] == 23950 and pick["extrinsic"] <= 25
    assert pick["intrinsic"] == round(pick["ltp"] - pick["extrinsic"], 2)


def test_long_steps_deeper_when_near_strike_is_rich():
    spot = 24000.0
    # Make near-money strikes expensive (high extrinsic); only deep strikes are cheap.
    def call_ltp(s):
        intrinsic = max(spot - s, 0)
        extrinsic = max(0, (s - 23000) / 5.0)     # 23000->0, 23200->40, 23900->180
        return intrinsic + extrinsic
    t = _table(spot, call_ltp, lambda s: 50.0)
    pick = select_strike(t, spot, "long", max_extrinsic=25.0)
    # First strike (scanning down from 23950) with extrinsic <= 25 is 23100 (extrinsic 20).
    assert pick["strike"] == 23100 and pick["extrinsic"] <= 25


def test_fallback_lowest_extrinsic_when_none_qualify():
    spot = 24000.0
    # Every ITM call carries > 25 extrinsic; agent falls back to the lowest one.
    def call_ltp(s):
        return max(spot - s, 0) + 40.0            # constant 40 extrinsic everywhere
    t = _table(spot, call_ltp, lambda s: 50.0)
    pick = select_strike(t, spot, "long", max_extrinsic=25.0)
    assert pick is not None and round(pick["extrinsic"], 0) == 40


def test_short_is_mirrored_on_the_put_side():
    spot = 24000.0
    # PE is ITM ABOVE spot; cheap (low extrinsic) puts up top.
    def put_ltp(s):
        intrinsic = max(s - spot, 0)
        extrinsic = max(0, (24500 - s) / 100.0) if s > spot else 0  # nearer-money richer
        return intrinsic + extrinsic
    t = _table(spot, lambda s: max(spot - s, 0) + 50, put_ltp)
    pick = select_strike(t, spot, "short")
    assert pick["right"] == "PE" and pick["strike"] > spot


def test_window_cap_and_empty():
    spot = 24000.0
    t = _table(spot, lambda s: max(spot - s, 0) + 5, lambda s: max(s - spot, 0) + 5)
    pick = select_strike(t, spot, "long", max_itm=1000)
    assert spot - pick["strike"] <= 1000            # never deeper than the window
    # flat / no chain -> None
    assert select_strike(t, spot, "flat") is None
    assert select_strike(pd.DataFrame(), spot, "long") is None
