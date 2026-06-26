"""The self-managed exit decision core (pure)."""

from __future__ import annotations

from execution.exit_manager import evaluate_exit


def _pos(**kw):
    base = dict(direction="long", entry=24000.0, stop=23950.0, target=24100.0,
                tsl_points=None, peak=None, entry_filled=True)
    base.update(kw)
    return base


def test_hold_until_entry_filled():
    assert evaluate_exit(_pos(entry_filled=False), 23000.0)["action"] == "hold"


def test_long_stop_hit():
    d = evaluate_exit(_pos(), 23950.0)
    assert d["action"] == "exit" and d["reason"] == "stop"


def test_long_target_hit():
    d = evaluate_exit(_pos(), 24100.0)
    assert d["action"] == "exit" and d["reason"] == "target"


def test_long_holds_inside_band():
    assert evaluate_exit(_pos(), 24010.0)["action"] == "hold"


def test_short_mirror():
    p = _pos(direction="short", entry=24000.0, stop=24050.0, target=23900.0)
    assert evaluate_exit(dict(p), 24050.0)["reason"] == "stop"
    assert evaluate_exit(dict(p), 23900.0)["reason"] == "target"
    assert evaluate_exit(dict(p), 23990.0)["action"] == "hold"


def test_tsl_raises_stop_then_exits_on_pullback():
    p = _pos(stop=23950.0, target=99999.0, tsl_points=20.0)   # target out of the way
    # price advances → trailing stop ratchets up
    d = evaluate_exit(p, 24080.0)
    assert d["action"] == "update" and d["new_stop"] == 24060.0
    p["stop"] = d["new_stop"]                                  # caller persists the raised stop
    # a further advance trails higher again
    d2 = evaluate_exit(p, 24090.0)
    assert d2["action"] == "update" and d2["new_stop"] == 24070.0
    p["stop"] = d2["new_stop"]
    # a pullback through the trailed stop fires the exit
    d3 = evaluate_exit(p, 24069.0)
    assert d3["action"] == "exit" and d3["reason"] == "stop"


def test_tsl_never_loosens():
    # Stop already tighter (24075) than the trail would set (peak 24080 - 20 = 24060):
    # the trail must NOT lower it back down. Price 24076 is above the stop (no stop-hit).
    p = _pos(stop=24075.0, target=99999.0, tsl_points=20.0, peak=24080.0)
    out = evaluate_exit(p, 24076.0)
    assert out["action"] == "hold"               # never an "update" that loosens the stop
