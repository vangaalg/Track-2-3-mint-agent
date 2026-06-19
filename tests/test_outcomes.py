"""Outcome settlement + the process×outcome 2x2 grading (the learning loop)."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from journal.outcomes import grade_process, settle, settle_log, matrix_summary
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


def test_settle_log_persists_and_summary(tmp_path):
    path = tmp_path / "d.jsonl"
    path.write_text(json.dumps(_rec("approved", "enter")) + "\n")
    bars = _bars([100, 100.5, 103], [100, 101, 103], [99.6, 100, 102])
    out = settle_log(path, {"3min": bars})
    assert out[0]["outcome"]["status"] == "win"
    assert "outcome" in path.read_text()                 # persisted
    s = matrix_summary(out)
    assert s["cells"]["deserved"] == 1 and s["n_settled"] == 1


def test_memory_warns_on_dangerous_win():
    decisions = [
        {"decision": "approved", "matrix": "dangerous",
         "proposal": {"recommendation": "stand_down", "ts": "t", "instrument": "NIFTY",
                      "direction": "long"}, "outcome": {"status": "win"}},
    ]
    mem = distill_memory(decisions)
    assert "dangerous" in mem.lower()
    assert "Session-002 trap" in mem
