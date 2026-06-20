"""Backtest aggregation + the end-to-end engine over synthetic frames (offline)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from scoring.backtest import aggregate, run_backtest, report_text
from feeds.snapshot import build_snapshot
from indicators.directional import journal_mtf_config


def test_aggregate_overall_and_breakdowns():
    rows = [
        {"direction": "long", "date": "2024-01-01", "outcome": "win", "points": 20.0},
        {"direction": "long", "date": "2024-01-01", "outcome": "loss", "points": -10.0},
        {"direction": "short", "date": "2024-01-02", "outcome": "win", "points": 15.0},
        {"direction": "short", "date": "2024-01-02", "outcome": "open", "points": 3.0},
    ]
    rep = aggregate(rows, lot_size=75, lots=1)
    o = rep["overall"]
    assert o["n"] == 4 and o["wins"] == 2 and o["losses"] == 1 and o["open"] == 1
    assert o["hit_rate"] == round(2 / 3, 3)
    assert o["net_points"] == 28.0 and o["net_rupees"] == 28.0 * 75
    assert o["avg_win"] == 17.5 and o["avg_loss"] == -10.0
    assert o["profit_factor"] == 3.5            # 35 won / 10 lost
    assert rep["by_direction"]["long"]["net_points"] == 10.0
    assert rep["by_direction"]["short"]["hit_rate"] == 1.0
    assert [d["date"] for d in rep["by_day"]] == ["2024-01-01", "2024-01-02"]


def test_aggregate_empty_is_safe():
    rep = aggregate([], lot_size=75, lots=1)
    assert rep["overall"]["n"] == 0 and rep["overall"]["hit_rate"] is None
    assert rep["overall"]["net_points"] == 0 and rep["by_day"] == []


def _synth_1m(days=3):
    rng = np.random.default_rng(0)
    frames, start = [], pd.Timestamp("2024-01-01 09:15", tz="Asia/Kolkata")
    for d in range(days):
        idx = pd.date_range(start + pd.Timedelta(days=d), periods=375, freq="1min", tz="Asia/Kolkata")
        p = 24000 + np.cumsum(rng.standard_normal(len(idx)))
        frames.append(pd.DataFrame({"open": p, "high": p + 3, "low": p - 3, "close": p,
                                    "volume": rng.integers(100, 1000, len(idx))}, index=idx))
    df = pd.concat(frames); df.index.name = "datetime"; return df


def _synth_daily():
    rng = np.random.default_rng(1)
    idx = pd.date_range("2023-11-01", periods=80, freq="1D", tz="Asia/Kolkata")
    p = 24000 + np.cumsum(rng.standard_normal(80) * 20)
    df = pd.DataFrame({"open": p, "high": p + 30, "low": p - 30, "close": p,
                       "volume": rng.integers(1000, 5000, 80)}, index=idx)
    df.index.name = "datetime"; return df


def test_run_backtest_shape_and_consistency():
    snap = build_snapshot("NIFTY", _synth_1m(3), _synth_daily(), mtf_cfg=journal_mtf_config())
    out = run_backtest(snap, lots=1)
    trigs, rep = out["triggers"], out["report"]
    # report is internally consistent with the trigger list it summarised
    assert rep["overall"]["n"] == len(trigs)
    assert (rep["overall"]["wins"] + rep["overall"]["losses"]
            + rep["overall"]["open"]) == len(trigs)
    # every trigger carries the fields the backtest aggregates on
    for t in trigs:
        assert t["outcome"] in ("win", "loss", "open") and "points" in t and "date" in t
    assert "OVERALL" in report_text("NIFTY", rep)        # renders without error
