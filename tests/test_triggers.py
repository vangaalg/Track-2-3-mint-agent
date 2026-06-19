"""Intraday trigger replay — entry detection + outcome simulation (synthetic)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from analysis.trade1 import trade1_levels
from analysis.triggers import replay_today


class _StubMTF:
    """Stand-in MTF config; the resolver is monkeypatched, so this is just a token."""


def _frames(closes, highs=None, lows=None):
    idx = pd.date_range("2026-06-23 09:15", periods=len(closes), freq="3min",
                        tz="Asia/Kolkata")
    c = np.asarray(closes, float)
    h = np.asarray(highs, float) if highs is not None else c + 1
    lo = np.asarray(lows, float) if lows is not None else c - 1
    return pd.DataFrame({"open": c, "high": h, "low": lo, "close": c,
                         "volume": 100.0}, index=idx)


def test_trade1_levels_long_geometry():
    levels = {"ema_45": 99.0, "supertrend": 98.0, "cpr_pivot": 99.5,
              "cpr_tc": 102.0, "cpr_bc": 97.0}
    stop, target, rr = trade1_levels("long", 100.0, levels)
    assert stop == 99.5 and target == 102.0          # nearest support / resistance
    assert rr == round((102 - 100) / (100 - 99.5), 2)


def test_replay_today_win_and_loss(monkeypatch):
    import analysis.triggers as trig
    # 6 bars: flat, flat, LONG entry @ bar2 (close 100), then runs up to target 102.
    closes = [100, 100, 100, 100.5, 101.5, 103]
    highs = [100, 100, 100, 101, 102.2, 103]   # target 102 hit at bar4
    lows = [99, 99, 99, 100, 101, 102]
    frames = _frames(closes, highs, lows)
    # feats carry the levels the entry bar reads (stop 99.5 -> risk 0.5, target 102).
    feats = frames.assign(ema_45=99.0, supertrend=98.0, cpr_pivot=99.5,
                          cpr_tc=102.0, cpr_bc=97.0)
    calls = pd.Series(["flat", "flat", "long", "long", "long", "long"], index=frames.index)
    monkeypatch.setattr(trig, "resolve_direction_mtf", lambda f, c: calls)

    out = replay_today({"3min": feats}, {"3min": frames}, cfg=_StubMTF(),
                       size_lots=75, lot_size=75)
    assert out["summary"]["n"] == 1
    t = out["last"]
    assert t["direction"] == "long" and t["entry"] == 100.0
    assert t["outcome"] == "win" and t["points"] == 2.0
    assert t["rupees"] == 2.0 * 75 * 75
    assert out["summary"]["wins"] == 1 and out["summary"]["hit_rate"] == 1.0


def test_replay_today_stop_out(monkeypatch):
    import analysis.triggers as trig
    closes = [100, 100, 100, 99.8, 99.4, 99.0]
    lows = [99, 99, 99, 99.4, 99.0, 98.5]   # stop 99.5 hit at bar3
    highs = [100, 100, 100, 100, 99.8, 99.4]
    frames = _frames(closes, highs, lows)
    feats = frames.assign(ema_45=99.0, supertrend=98.0, cpr_pivot=99.5,
                          cpr_tc=102.0, cpr_bc=97.0)
    calls = pd.Series(["flat", "flat", "long", "long", "long", "long"], index=frames.index)
    monkeypatch.setattr(trig, "resolve_direction_mtf", lambda f, c: calls)

    out = replay_today({"3min": feats}, {"3min": frames}, cfg=_StubMTF())
    t = out["last"]
    assert t["outcome"] == "loss" and t["points"] == -0.5
    assert out["summary"]["losses"] == 1


def test_replay_no_session_data():
    out = replay_today({}, {})
    assert out["summary"]["n"] == 0 and out["triggers"] == []
