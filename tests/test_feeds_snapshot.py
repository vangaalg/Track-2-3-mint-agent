"""Feeds layer — snapshot assembly + OI/macro summaries (offline, synthetic)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from feeds.snapshot import build_snapshot, assemble_ladder
from feeds.oi import summarise_chain
from feeds.macro import summarise_quote, fetch_macro


def _synth_1m(days: int = 2) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    frames = []
    start = pd.Timestamp("2024-01-01 09:15", tz="Asia/Kolkata")
    for d in range(days):
        idx = pd.date_range(start + pd.Timedelta(days=d), periods=375, freq="1min",
                            tz="Asia/Kolkata")
        p = 100 + np.cumsum(rng.standard_normal(len(idx)) * 0.05)
        frames.append(pd.DataFrame(
            {"open": p, "high": p + 0.1, "low": p - 0.1, "close": p,
             "volume": rng.integers(100, 1000, len(idx))}, index=idx))
    df = pd.concat(frames)
    df.index.name = "datetime"
    return df


def _synth_daily(days: int = 60) -> pd.DataFrame:
    rng = np.random.default_rng(1)
    idx = pd.date_range("2023-11-01", periods=days, freq="1D", tz="Asia/Kolkata")
    p = 100 + np.cumsum(rng.standard_normal(days))
    df = pd.DataFrame({"open": p, "high": p + 1, "low": p - 1, "close": p,
                       "volume": rng.integers(1000, 5000, days)}, index=idx)
    df.index.name = "datetime"
    return df


def test_assemble_ladder_has_full_tf_set():
    frames = assemble_ladder(_synth_1m(), _synth_daily(), anchor="9h15min")
    assert set(frames) == {"1min", "3min", "15min", "30min", "60min", "1day", "1week", "1month"}
    # resampled bars aggregate correctly: 3m open == first 1m open of the bin.
    assert frames["3min"]["open"].iloc[0] == frames["1min"]["open"].iloc[0]


def test_build_snapshot_degrades_without_oi_macro():
    snap = build_snapshot("NIFTY", _synth_1m(), _synth_daily(), anchor="9h15min")
    assert snap.instrument == "NIFTY"
    assert snap.chart_read["mtf_call"] in {"long", "short", "flat"}
    assert isinstance(snap.spot, float)
    assert {"3min", "15min", "30min", "60min", "1day", "1week"} <= set(snap.feats)
    conf = snap.chart_read["mtf_confidence"]
    assert isinstance(conf, int) and 0 <= conf <= 5
    assert isinstance(snap.chart_read["mtf_confidence_breakdown"], dict)
    assert snap.oi is None and snap.macro is None
    assert any("oi" in n for n in snap.notes) and any("macro" in n for n in snap.notes)


def test_build_snapshot_with_injected_oi_and_macro():
    chain = pd.DataFrame({
        "strike": [99, 100, 101, 102],
        "call_oi": [10, 20, 80, 30],   # call wall at 101
        "put_oi": [70, 40, 10, 5],     # put shelf at 99
    })
    snap = build_snapshot(
        "NIFTY", _synth_1m(), _synth_daily(), anchor="9h15min",
        oi_fetch_fn=lambda inst: chain,
        macro_quote_fn=lambda sym: {"price": 100.0, "prev_close": 99.0},
    )
    assert snap.oi["call_wall"]["strike"] == 101.0
    assert snap.oi["put_shelf"]["strike"] == 99.0
    assert snap.macro["usd_inr"]["change_pct"] is not None


def test_summarise_chain_pcr_and_levels():
    chain = pd.DataFrame({
        "strike": [100, 101, 102, 103],
        "call_oi": [5, 10, 50, 20],
        "put_oi": [60, 30, 10, 5],
    })
    s = summarise_chain(chain, spot=101.4)
    assert s["call_wall"]["strike"] == 102.0
    assert s["put_shelf"]["strike"] == 100.0
    assert s["atm"] == 101.0
    assert s["pcr"] == (105 / 85)


def test_summarise_quote_change():
    assert summarise_quote({"price": 110.0, "prev_close": 100.0})["change_pct"] == 10.0
    assert summarise_quote({"price": 110.0, "prev_close": None})["change_pct"] is None
    assert fetch_macro(quote_fn=None) is None
