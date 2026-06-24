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
    # ₹ is sized by the row's own conviction (size_for_confidence), not a flat 75 lots.
    from analysis.trade1 import size_for_confidence
    assert t["size_lots"] == size_for_confidence(t["mtf_confidence"])
    assert t["rupees"] == 2.0 * 75 * t["size_lots"]
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


def test_replay_today_one_position_flips_on_reversal(monkeypatch):
    import analysis.triggers as trig
    # long @ bar2 (close 100); a SHORT trigger fires @ bar4 (close 101) before the long
    # hits its 99.6 stop / 102 target -> the long is FLATTENED at the short's entry.
    closes = [100, 100, 100, 100.5, 101, 100.5, 100]
    highs = [100, 100, 100, 100.8, 101.2, 100.8, 100.3]
    lows = [99.6, 99.6, 99.6, 100.2, 100.5, 99.8, 99.6]
    frames = _frames(closes, highs, lows)
    feats = frames.assign(ema_45=99.0, supertrend=98.0, cpr_pivot=99.5,
                          cpr_tc=102.0, cpr_bc=97.0)
    calls = pd.Series(["flat", "flat", "long", "long", "short", "short", "short"],
                      index=frames.index)
    monkeypatch.setattr(trig, "resolve_direction_mtf", lambda f, c: calls)

    op = replay_today({"3min": feats}, {"3min": frames}, cfg=_StubMTF(), one_position=True)
    assert op["summary"]["n"] == 2
    long_row = op["triggers"][0]
    # the long closes at the short's entry (101), NOT held to its own stop/target
    assert long_row["direction"] == "long" and long_row["outcome"] == "flip"
    assert long_row["exit"] == 101.0 and long_row["points"] == 1.0 and long_row["exit_ts"]
    assert op["triggers"][1]["direction"] == "short"           # the reversed position

    # default (independent) keeps the old behavior — no "flip", no exit field
    indep = replay_today({"3min": feats}, {"3min": frames}, cfg=_StubMTF())
    assert indep["triggers"][0]["outcome"] != "flip" and "exit" not in indep["triggers"][0]


def test_resolve_window_stops_before_next_trigger():
    import analysis.triggers as trig
    frames = _frames([100, 100, 100, 99.0, 99.0], highs=[100, 100, 101, 101, 101],
                     lows=[100, 100, 99.4, 98.5, 98.5])
    ts, nxt = frames.index[1], frames.index[4]      # window spans bars 2,3
    # long entry 100, stop 99.5 hit at bar3 (low 99.4) BEFORE the next trigger -> loss
    outcome, exit_px, pts, exit_ts = trig._resolve_window(frames, ts, nxt, "long", 100.0, 99.5, 103.0)
    assert outcome == "loss" and exit_px == 99.5 and pts == -0.5
    assert exit_ts == frames.index[2]


def test_replay_no_session_data():
    out = replay_today({}, {})
    assert out["summary"]["n"] == 0 and out["triggers"] == []


def test_replay_today_session_date_picks_a_prior_day(monkeypatch):
    import analysis.triggers as trig
    # two sessions; each has a LONG entry on its 3rd bar
    day1 = pd.date_range("2024-01-02 09:15", periods=6, freq="3min", tz="Asia/Kolkata")
    day2 = pd.date_range("2024-01-03 09:15", periods=6, freq="3min", tz="Asia/Kolkata")
    idx = day1.append(day2)
    c = np.array([100, 100, 100, 100.5, 101.5, 103] * 2, float)
    frames = pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c,
                           "volume": 100.0}, index=idx)
    feats = frames.assign(ema_45=99.0, supertrend=98.0, cpr_pivot=99.5,
                          cpr_tc=102.0, cpr_bc=97.0)
    calls = pd.Series(["flat", "flat", "long", "long", "long", "long"] * 2, index=idx)
    monkeypatch.setattr(trig, "resolve_direction_mtf", lambda f, c: calls)

    latest = replay_today({"3min": feats}, {"3min": frames}, cfg=_StubMTF())
    assert latest["session"] == "2024-01-03"            # default = latest session
    prior = replay_today({"3min": feats}, {"3min": frames}, cfg=_StubMTF(),
                         session_date="2024-01-02")
    assert prior["session"] == "2024-01-02" and prior["summary"]["n"] == 1
    assert prior["last"]["ts"].startswith("2024-01-02")

    # A session with NO bars (e.g. TODAY pre-market) returns an empty result, not an error.
    empty = replay_today({"3min": feats}, {"3min": frames}, cfg=_StubMTF(),
                         session_date="2024-01-09")
    assert empty["session"] == "2024-01-09" and empty["summary"]["n"] == 0
    assert empty["triggers"] == [] and empty["last"] is None


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


def test_target_driven_derives_stop_from_rr_long():
    # nearest resist ahead = 130; reward 30 -> risk 20 -> stop 80, rr fixed at 1.5
    stop, target, rr = trade1_levels("long", 100.0, {"cpr_tc": 130.0}, target_driven=True)
    assert target == 130.0 and rr == 1.5 and abs(stop - 80.0) < 1e-9


def test_target_driven_derives_stop_from_rr_short():
    stop, target, rr = trade1_levels("short", 100.0, {"cpr_bc": 70.0}, target_driven=True)
    assert target == 70.0 and rr == 1.5 and abs(stop - 120.0) < 1e-9


def test_target_driven_falls_back_when_no_objective_ahead():
    # no resistance above entry -> stop-driven fallback (session-low stop + 1.5R target)
    stop, target, rr = trade1_levels("long", 100.0, {"session_low": 90.0}, target_driven=True)
    assert stop == 90.0 and target == 115.0 and rr == 1.5


def test_min_stop_floor_target_driven():
    # tiny structural target -> min_stop widens the stop and pushes the target out to keep R:R
    stop, target, rr = trade1_levels("long", 100.0, {"cpr_tc": 100.3},
                                     target_driven=True, min_stop=20)
    assert rr == 1.5 and abs(stop - 80.0) < 1e-9 and abs(target - 130.0) < 1e-9


def test_min_stop_floor_stop_driven():
    # session low only 1pt below -> floored to 20, target to 1.5R
    stop, target, rr = trade1_levels("long", 100.0, {"session_low": 99.0}, min_stop=20)
    assert abs(stop - 80.0) < 1e-9 and abs(target - 130.0) < 1e-9 and rr == 1.5
