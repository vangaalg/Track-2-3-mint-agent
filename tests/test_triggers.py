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
    lows = [99, 99, 99, 99.4, 99.0, 98.5]   # session low 99.0 = the long's stop
    highs = [100, 100, 100, 100, 99.8, 99.4]
    frames = _frames(closes, highs, lows)
    feats = frames.assign(ema_45=99.0, supertrend=98.0, cpr_pivot=99.5,
                          cpr_tc=102.0, cpr_bc=97.0)
    calls = pd.Series(["flat", "flat", "long", "long", "long", "long"], index=frames.index)
    monkeypatch.setattr(trig, "resolve_direction_mtf", lambda f, c: calls)

    out = replay_today({"3min": feats}, {"3min": frames}, cfg=_StubMTF())
    t = out["last"]
    # stop = the session low (99.0), hit at bar4 -> loss of 1.0 point
    assert t["stop"] == 99.0 and t["outcome"] == "loss" and t["points"] == -1.0
    assert out["summary"]["losses"] == 1


def test_replay_no_session_data():
    out = replay_today({}, {})
    assert out["summary"]["n"] == 0 and out["triggers"] == []


def test_rr_floor_pushes_close_long_target_out():
    # Structural resist only 2 pts above entry, stop 20 below -> floored to 1.5R.
    stop, target, rr = trade1_levels("long", 100.0,
                                     {"session_low": 80.0, "cpr_tc": 102.0})
    assert stop == 80.0 and rr == 1.5
    assert target == 100.0 + 1.5 * 20.0          # 130, not the 2-pt structural target


def test_rr_floor_short_side():
    stop, target, rr = trade1_levels("short", 100.0,
                                     {"session_high": 120.0, "cpr_bc": 98.0})
    assert stop == 120.0 and rr == 1.5
    assert target == 100.0 - 1.5 * 20.0          # 70


def test_realistic_dedupes_and_marks_eod(monkeypatch):
    import analysis.triggers as trig
    idx = pd.date_range("2024-01-01 09:18", periods=6, freq="3min", tz="Asia/Kolkata")
    frame = pd.DataFrame({
        "open": 100.0,
        "high": [101.0, 101.0, 103.0, 101.0, 103.0, 101.0],
        "low":  [95.0, 98.0, 97.0, 98.0, 97.0, 98.0],
        "close": 100.0, "volume": 100.0}, index=idx)
    calls = pd.Series(["flat", "long", "flat", "long", "flat", "flat"], index=idx)
    monkeypatch.setattr(trig, "resolve_direction_mtf", lambda f, c: calls)
    monkeypatch.setattr(trig, "mtf_ema45_confidence",
                        lambda f, c: (pd.Series([0] * 6, index=idx), None))
    feats = {"3min": pd.DataFrame({"x": 0.0}, index=idx)}
    frames = {"3min": frame}

    # raw enumeration: both long flips counted, mark-to-close labelled "open"
    raw = trig.list_triggers(feats, frames, cfg=_StubMTF())
    assert len(raw) == 2 and all(t["outcome"] == "open" for t in raw)
    assert "exit_ts" not in raw[0]

    # realistic: the 2nd trigger fires while the 1st is still open -> deduped to one,
    # and the unhit trade is an explicit EOD exit (not "open")
    real = trig.list_triggers(feats, frames, cfg=_StubMTF(), realistic=True)
    assert len(real) == 1
    assert real[0]["outcome"] == "eod"
    assert real[0]["exit_ts"].startswith("2024-01-01T09:33")
    assert "exit" in real[0]
