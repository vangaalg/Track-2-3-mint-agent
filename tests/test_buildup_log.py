"""CLEAR bull/bear transition log — state grading + episode collapse + duration."""

from __future__ import annotations

import pandas as pd

from feeds.buildup_log import clear_episodes, collect, grade_state


def test_grade_state_thresholds():
    assert grade_state(0.55) == "clear_bull"
    assert grade_state(0.25) == "bull"
    assert grade_state(0.05) == "neutral"
    assert grade_state(-0.25) == "bear"
    assert grade_state(-0.55) == "clear_bear"
    assert grade_state(None) == "neutral"          # NaN/None → neutral, never a guess
    assert grade_state(float("nan")) == "neutral"
    assert grade_state(0.4) == "clear_bull" and grade_state(-0.4) == "clear_bear"  # inclusive bar


def _series(pairs):
    """pairs = [(ts, score), ...] → a ts-indexed summary frame (spot filled in)."""
    df = pd.DataFrame([{"ts": t, "buildup_score": s, "spot": 24000 + i}
                       for i, (t, s) in enumerate(pairs)])
    return df.set_index("ts")


def test_bull_to_clear_bull_episode_holds_10_min():
    """The trader's example: bull @09:35 → clear_bull @09:40, still clear @09:50 ⇒ one
    holding episode, from_state=bull, duration 10 min."""
    df = _series([
        ("2026-08-06T09:35:00+05:30", 0.2),    # bull
        ("2026-08-06T09:40:00+05:30", 0.5),    # → clear_bull (start)
        ("2026-08-06T09:45:00+05:30", 0.6),    # still clear (peak)
        ("2026-08-06T09:50:00+05:30", 0.45),   # still clear (last row)
    ])
    eps = clear_episodes(df, symbol="NIFTY")
    assert len(eps) == 1
    e = eps[0]
    assert e["state"] == "clear_bull" and e["bias"] == "bullish"
    assert e["from_state"] == "bull" and e["to_state"] is None
    assert e["holding"] is True
    assert e["date"] == "2026-08-06"
    assert e["start"].startswith("2026-08-06T09:40")
    assert e["duration_min"] == 10.0           # recorded span 09:40 → 09:50 (live now-turn is client-side)
    assert e["peak_score"] == 0.6 and e["samples"] == 3
    assert e["spot_at_start"] == 24001.0


def test_episode_breaks_on_the_date_boundary():
    """A clear lean at yesterday's close and again at today's open is TWO episodes, not one
    20-hour hold — a lean cannot carry across an overnight market close."""
    df = _series([
        ("2026-08-05T15:25:00+05:30", 0.5),    # yesterday, clear (closes the session)
        ("2026-08-06T09:15:00+05:30", 0.5),    # today, clear again (fresh episode)
        ("2026-08-06T09:30:00+05:30", 0.55),   # today, holding
    ])
    eps = clear_episodes(df, symbol="NIFTY")
    assert len(eps) == 2
    assert eps[0]["date"] == "2026-08-05" and eps[0]["holding"] is False
    assert eps[0]["to_state"] is None          # session boundary, not a real break
    assert eps[1]["date"] == "2026-08-06" and eps[1]["holding"] is True
    assert eps[1]["from_state"] is None        # first sample of today's session
    assert eps[1]["duration_min"] == 15.0      # 09:15 → 09:30 within today


def test_closed_episode_records_end_and_to_state():
    """A clear run that ends before the last row is closed: holding False, to_state set."""
    df = _series([
        ("2026-08-06T10:00:00+05:30", -0.5),   # clear_bear (start)
        ("2026-08-06T10:15:00+05:30", -0.6),
        ("2026-08-06T10:30:00+05:30", -0.2),   # bear → breaks the clear run
        ("2026-08-06T10:45:00+05:30", 0.0),
    ])
    eps = clear_episodes(df, symbol="BANKNIFTY")
    assert len(eps) == 1
    e = eps[0]
    assert e["state"] == "clear_bear" and e["holding"] is False
    assert e["to_state"] == "bear" and e["duration_min"] == 15.0   # 10:00 → 10:15
    assert e["end"].startswith("2026-08-06T10:15")


def test_two_separate_episodes_with_a_gap():
    """Clear → neutral → clear again = two distinct episodes (a re-entry, not one run)."""
    df = _series([
        ("2026-08-06T09:20:00+05:30", 0.5),    # clear_bull #1
        ("2026-08-06T09:35:00+05:30", 0.1),    # neutral (breaks it)
        ("2026-08-06T09:50:00+05:30", 0.5),    # clear_bull #2 (start)
        ("2026-08-06T10:05:00+05:30", 0.55),   # holding
    ])
    eps = clear_episodes(df, symbol="NIFTY")
    assert len(eps) == 2
    assert eps[0]["holding"] is False and eps[0]["to_state"] == "neutral"
    assert eps[1]["holding"] is True and eps[1]["from_state"] == "neutral"


def test_no_clear_lean_returns_nothing():
    df = _series([("2026-08-06T09:20:00+05:30", 0.1), ("2026-08-06T09:35:00+05:30", -0.2)])
    assert clear_episodes(df, symbol="NIFTY") == []
    assert clear_episodes(None) == [] and clear_episodes(pd.DataFrame()) == []


def test_collect_sorts_holding_first_then_newest():
    frames = {
        "AAA": _series([("2026-08-06T09:20:00+05:30", 0.5),      # closed early episode
                        ("2026-08-06T09:35:00+05:30", 0.0)]),
        "BBB": _series([("2026-08-06T11:00:00+05:30", -0.5),     # holding, latest start
                        ("2026-08-06T11:15:00+05:30", -0.6)]),
        "CCC": None,                                             # no data → nothing
    }
    eps = collect(["AAA", "BBB", "CCC"], lambda s: frames.get(s))
    assert eps[0]["symbol"] == "BBB" and eps[0]["holding"] is True   # holding surfaces first
    assert {e["symbol"] for e in eps} == {"AAA", "BBB"}


def test_collect_isolates_a_failing_loader():
    def load(s):
        if s == "BAD":
            raise RuntimeError("store down")
        return _series([("2026-08-06T09:40:00+05:30", 0.5),
                        ("2026-08-06T09:55:00+05:30", 0.5)])
    eps = collect(["BAD", "GOOD"], load)
    assert [e["symbol"] for e in eps] == ["GOOD"]     # the failing one never blocks the rest
