"""Backtest aggregation + the end-to-end engine over synthetic frames (offline)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from scoring.backtest import (aggregate, run_backtest, report_text, make_claude_filter,
                              clamp_levels)
from feeds.snapshot import build_snapshot
from indicators.directional import journal_mtf_config
from agent.read import ClaudeRead


def test_aggregate_overall_and_breakdowns():
    rows = [
        {"direction": "long", "date": "2024-01-01", "outcome": "win", "points": 20.0},
        {"direction": "long", "date": "2024-01-01", "outcome": "loss", "points": -10.0},
        {"direction": "short", "date": "2024-01-02", "outcome": "win", "points": 15.0},
        {"direction": "short", "date": "2024-01-02", "outcome": "eod", "points": 3.0},
    ]
    rep = aggregate(rows, lot_size=75, lots=1)
    o = rep["overall"]
    assert o["n"] == 4 and o["wins"] == 2 and o["losses"] == 1 and o["eod"] == 1
    assert o["hit_rate"] == round(2 / 3, 3)            # target-vs-stop only
    assert o["net_points"] == 28.0 and o["net_rupees"] == 28.0 * 75
    assert o["eod_points"] == 3.0
    assert o["avg_win"] == 17.5 and o["avg_loss"] == -10.0
    assert o["expectancy"] == round(28.0 / 4, 2)        # net per trade, all exits
    assert o["profit_factor"] == round(38.0 / 10.0, 2)  # gains incl eod (20+15+3) / losses (10)
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
            + rep["overall"]["eod"]) == len(trigs)
    # realistic backtest: every trigger is a realised exit (win/loss/eod) + exit fields
    for t in trigs:
        assert t["outcome"] in ("win", "loss", "eod") and "points" in t and "date" in t
        assert "exit_ts" in t and "exit" in t
    assert "OVERALL" in report_text("NIFTY", rep)        # renders without error


def _stub_read(rec):
    return ClaudeRead(agrees_with_engine=True, chart_analysis="ca", oi_analysis="oa",
                      where_moving="wm", right_trade="rt", challenge="ch",
                      recommendation=rec, confidence=3, key_risk="kr")


def test_claude_filter_tags_and_splits_report():
    snap = build_snapshot("NIFTY", _synth_1m(3), _synth_daily(), mtf_cfg=journal_mtf_config())
    # stub filter: take longs, skip shorts
    out = run_backtest(snap, lots=1,
                       claude_filter=lambda t: "enter" if t["direction"] == "long" else "stand_down")
    assert out["filtered"] is not None
    assert all("claude" in t for t in out["triggers"])
    taken = [t for t in out["triggers"] if t["claude"] == "enter"]
    assert out["filtered"]["overall"]["n"] == len(taken)
    assert all(t["direction"] == "long" for t in taken)
    # the filtered block renders
    assert "CLAUDE-FILTERED" in report_text("NIFTY", out["report"], filtered=out["filtered"])


def test_make_claude_filter_uses_completer():
    base, daily = _synth_1m(3), _synth_daily()
    fn = make_claude_filter("NIFTY", base, daily,
                            completer=lambda system, user: _stub_read("enter"))
    cf = fn({"ts": base.index[800].isoformat()})
    assert cf["verdict"] == "enter" and "target" in cf and "stop" in cf


def test_run_backtest_without_filter_has_no_filtered():
    snap = build_snapshot("NIFTY", _synth_1m(3), _synth_daily(), mtf_cfg=journal_mtf_config())
    out = run_backtest(snap, lots=1)
    assert out["filtered"] is None


def test_clamp_levels_guardrails():
    # valid long: target above, stop below, rr exactly at the floor
    assert clamp_levels("long", 24000, 24300, 23800) == (24300.0, 23800.0, 1.5)
    # rr below floor -> target pushed out to 1.5R (risk 200 -> reward 300 -> 24300)
    assert clamp_levels("long", 24000, 24050, 23800) == (24300.0, 23800.0, 1.5)
    # insane stop capped to 2% of price (risk 4000 -> 480 -> stop 23520)
    t, s, rr = clamp_levels("long", 24000, 25000, 20000)
    assert s == 23520.0 and rr == round(1000 / 480, 2)
    # wrong side / missing -> unusable
    assert clamp_levels("long", 24000, 23900, 23800) == (None, None, None)
    assert clamp_levels("short", 24000, 24300, 23800) == (None, None, None)
    assert clamp_levels("long", 24000, None, 23800) == (None, None, None)


def test_claude_filter_trades_claude_levels():
    snap = build_snapshot("NIFTY", _synth_1m(3), _synth_daily(), mtf_cfg=journal_mtf_config())

    def cf(t):
        if t["direction"] == "long":
            return {"verdict": "enter", "target": t["entry"] + 300, "stop": t["entry"] - 200}
        return {"verdict": "enter", "target": t["entry"] - 300, "stop": t["entry"] + 200}

    out = run_backtest(snap, lots=1, claude_filter=cf)
    for t in out["triggers"]:
        if t["claude"] == "enter":
            assert t["claude_rr"] == 1.5 and "claude_target" in t and "claude_stop" in t


def test_make_claude_filter_tracks_errors():
    base, daily = _synth_1m(3), _synth_daily()

    def boom(system, user):
        raise RuntimeError("api down")

    fn = make_claude_filter("NIFTY", base, daily, completer=boom)
    cf = fn({"ts": base.index[800].isoformat(), "direction": "long"})
    assert cf["verdict"] == "stand_down" and cf.get("error") is True
    assert fn.state["errors"] == 1 and "api down" in fn.state["first_error"]


def test_min_stop_floors_backtest_stops():
    snap = build_snapshot("NIFTY", _synth_1m(3), _synth_daily(), mtf_cfg=journal_mtf_config())
    out = run_backtest(snap, lots=1, min_stop=20)
    for t in out["triggers"]:
        assert abs(t["entry"] - t["eng_stop"]) >= 20 - 1e-6     # no tiny stops


def test_atr_floor_widens_tightest_stop():
    snap = build_snapshot("NIFTY", _synth_1m(3), _synth_daily(), mtf_cfg=journal_mtf_config())
    base = run_backtest(snap, lots=1, atr_mult=0)["triggers"]
    atrd = run_backtest(snap, lots=1, atr_mult=3)["triggers"]
    # ATR floor lifts the tightest stop (dedup changes the set, so compare the minima)
    base_min = min(abs(t["entry"] - t["eng_stop"]) for t in base)
    atr_min = min(abs(t["entry"] - t["eng_stop"]) for t in atrd)
    assert atr_min > base_min
