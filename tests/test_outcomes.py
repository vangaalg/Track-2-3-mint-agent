"""Outcome settlement + the process×outcome 2x2 grading (the learning loop)."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from journal.outcomes import (
    grade_process, settle, settle_log, matrix_summary, conviction_breakdown,
    manual_exit_outcome)
from agent.memory import distill_memory


def _bars(closes, highs, lows):
    idx = pd.date_range("2026-06-23 11:00", periods=len(closes), freq="3min",
                        tz="Asia/Kolkata")
    return pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes,
                         "volume": 100.0}, index=idx)


def _rec(decision, recommendation, **prop):
    base = {"direction": "long", "entry": 100.0, "stop": 99.5, "target": 102.0,
            "size_lots": 75, "ts": "2026-06-23T11:00:00+05:30",
            "recommendation": recommendation}
    base.update(prop)
    return {"decision": decision, "proposal": base}


def test_manual_exit_outcome_long_win_short_loss():
    from analysis.trade1 import LOT_SIZE
    long_p = {"direction": "long", "entry": 24000.0, "size_lots": 2}
    o = manual_exit_outcome(long_p, 24050.0, "2026-06-23T13:00:00+05:30")
    assert o["status"] == "win" and o["points"] == 50.0 and o["exit"] == 24050.0
    assert o["rupees"] == round(50.0 * LOT_SIZE * 2, 0) and o["manual"] is True
    # a short exited ABOVE entry is a loss (negative points)
    short_p = {"direction": "short", "entry": 24000.0, "size_lots": 1}
    o2 = manual_exit_outcome(short_p, 24030.0, None)
    assert o2["status"] == "loss" and o2["points"] == -30.0


def test_grade_process():
    assert grade_process(_rec("approved", "enter")) == "good"
    assert grade_process(_rec("approved", "stand_down")) == "override"
    assert grade_process(_rec("rejected", "enter")) == "no_trade"


def test_settle_good_process_win_is_deserved():
    # price runs to target 102 -> win; approved enter -> good process -> deserved.
    bars = _bars([100, 100.5, 101.5, 103], [100, 101, 102.2, 103], [99.6, 100, 101, 102])
    recs, changed = settle([_rec("approved", "enter")], {"3min": bars})
    assert changed
    o = recs[0]["outcome"]
    assert o["status"] == "win" and o["points"] == 2.0 and o["rupees"] == 2.0 * 75 * 75
    assert recs[0]["matrix"] == "deserved"


def test_settle_override_win_is_dangerous():
    # approved a STAND_DOWN (override) that happened to win -> the 'dangerous' cell.
    bars = _bars([100, 101.5, 103], [100, 102.2, 103], [99.6, 101, 102])
    recs, _ = settle([_rec("approved", "stand_down")], {"3min": bars})
    assert recs[0]["matrix"] == "dangerous"


def test_settle_good_process_loss_is_accept():
    bars = _bars([100, 99.6, 99.0], [100, 99.9, 99.4], [99.4, 99.0, 98.5])  # stop 99.5 hit
    recs, _ = settle([_rec("approved", "enter")], {"3min": bars})
    assert recs[0]["outcome"]["status"] == "loss" and recs[0]["matrix"] == "accept"


def test_settle_past_session_auto_marks_to_close_at_eod():
    # An unresolved trade on a PAST session (neither stop nor target hit) is auto
    # marked-to-close at the session bell — win/loss by sign, eod-flagged, persisted.
    idx = pd.date_range("2024-01-02 11:00", periods=4, freq="3min", tz="Asia/Kolkata")
    bars = pd.DataFrame({"open": [100, 100.4, 100.7, 101], "high": [100.1, 100.5, 100.8, 101.1],
                         "low": [99.7, 100.1, 100.4, 100.7], "close": [100, 100.4, 100.7, 101],
                         "volume": 100.0}, index=idx)                 # drifts up, no stop/target
    recs, changed = settle([_rec("approved", "enter", ts="2024-01-02T11:00:00+05:30")],
                           {"3min": bars})
    assert changed
    o = recs[0]["outcome"]
    assert o["eod"] is True and o["status"] == "win" and o["points"] == 1.0
    assert recs[0]["matrix"] == "deserved"


def test_settle_live_session_stays_open(monkeypatch):
    # While the trade's own session is still in progress, an unresolved trade stays open
    # (we don't mark it to close mid-session).
    import journal.outcomes as oc
    monkeypatch.setattr(oc, "_session_live", lambda ts: True)
    bars = _bars([100, 100.2, 100.3, 100.4], [100.1, 100.3, 100.4, 100.5],
                 [99.7, 100.0, 100.1, 100.2])
    recs, changed = settle([_rec("approved", "enter")], {"3min": bars})
    assert not changed and recs[0].get("outcome") is None


def test_settle_resolution_is_session_bounded(monkeypatch):
    # A move that would only "hit" stop on the NEXT day must not leak across sessions:
    # the trade resolves within its own session (here: eod-close win on day one).
    import journal.outcomes as oc
    monkeypatch.setattr(oc, "_session_live", lambda ts: False)
    day1 = pd.date_range("2024-01-02 11:00", periods=3, freq="3min", tz="Asia/Kolkata")
    day2 = pd.date_range("2024-01-03 11:00", periods=3, freq="3min", tz="Asia/Kolkata")
    idx = day1.append(day2)
    close = [100, 100.5, 101, 98, 97, 96]              # day-2 crashes through stop 99.5
    bars = pd.DataFrame({"open": close, "high": [c + 0.1 for c in close],
                         "low": [c - 0.1 for c in close], "close": close, "volume": 100.0},
                        index=idx)
    recs, _ = settle([_rec("approved", "enter", ts="2024-01-02T11:00:00+05:30")], {"3min": bars})
    # resolved on day one (eod win) — the day-2 crash is a different session, ignored.
    assert recs[0]["outcome"]["status"] == "win" and recs[0]["outcome"]["eod"] is True


def test_settle_log_persists_and_summary(tmp_path):
    path = tmp_path / "d.jsonl"
    path.write_text(json.dumps(_rec("approved", "enter")) + "\n")
    bars = _bars([100, 100.5, 103], [100, 101, 103], [99.6, 100, 102])
    out = settle_log(path, {"3min": bars})
    assert out[0]["outcome"]["status"] == "win"
    assert "outcome" in path.read_text()                 # persisted
    s = matrix_summary(out)
    assert s["cells"]["deserved"] == 1 and s["n_settled"] == 1


def test_conviction_breakdown_groups_win_rate_by_conviction():
    decisions = [
        {"final_confidence": 5, "outcome": {"status": "win", "points": 40}},
        {"final_confidence": 5, "outcome": {"status": "win", "points": 20}},
        {"final_confidence": 2, "outcome": {"status": "loss", "points": -30}},
        {"final_confidence": 2, "outcome": {"status": "win", "points": 10}},
        {"final_confidence": 5, "outcome": {"status": "open"}},          # ignored (unsettled)
        {"proposal": {"mtf_confidence": 3}, "outcome": {"status": "loss", "points": -15}},  # blob fallback
    ]
    rows = conviction_breakdown(decisions)
    by = {r["conviction"]: r for r in rows}
    assert by[5]["n"] == 2 and by[5]["wins"] == 2 and by[5]["hit_rate"] == 1.0
    assert by[5]["net_points"] == 60 and by[5]["expectancy"] == 30
    assert by[2]["n"] == 2 and by[2]["hit_rate"] == 0.5 and by[2]["expectancy"] == -10
    assert by[3]["n"] == 1 and by[3]["losses"] == 1          # fell back to the proposal blob
    assert [r["conviction"] for r in rows] == [2, 3, 5]      # ordered low→high


def test_memory_warns_on_dangerous_win():
    decisions = [
        {"decision": "approved", "matrix": "dangerous",
         "proposal": {"recommendation": "stand_down", "ts": "t", "instrument": "NIFTY",
                      "direction": "long"}, "outcome": {"status": "win"}},
    ]
    mem = distill_memory(decisions)
    assert "dangerous" in mem.lower()
    assert "Session-002 trap" in mem
