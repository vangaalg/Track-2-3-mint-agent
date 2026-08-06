"""OI-buildup watchlist scan — per-instrument card + clear-first ordering."""

from __future__ import annotations

import pandas as pd

from feeds.buildup_scan import build_card, scan


def _row(bias, score, cw=24200, ps=23800, dom_c=24300, dom_p=23700):
    return {
        "ts": "2026-07-30T13:00:00+05:30", "spot": 24010.0,
        "buildup_bias": bias, "buildup_score": score,
        "call_writing": 5e5, "put_writing": 3e5,
        "call_wall_strike": cw, "put_shelf_strike": ps,
        "res_ext1": cw + 37, "sup_ext1": ps - 37,
        "buildup_call_strike": dom_c, "buildup_put_strike": dom_p, "pcr": 0.9,
    }


def test_card_clear_bearish_focuses_call_strike():
    c = build_card("NIFTY", _row("bearish", -0.6))
    assert c["has_data"] and c["clear"] is True and c["bias"] == "bearish"
    assert c["resistance"] == 24200 and c["support"] == 23800          # walls
    assert c["eor"] == 24200 and c["eor_ext"] == 24237                 # EOR / EOR+1
    assert c["eos"] == 23800 and c["eos_ext"] == 23763                 # EOS / EOS-1
    assert c["shift_strike"] == 24300                                  # bearish → call-writing strike


def test_card_clear_bullish_focuses_put_strike():
    c = build_card("BANKNIFTY", _row("bullish", 0.5))
    assert c["clear"] is True and c["shift_strike"] == 23700           # bullish → put-writing strike


def test_card_weak_lean_not_clear():
    c = build_card("INFY", _row("bearish", -0.2))
    assert c["has_data"] and c["clear"] is False                       # |score| 0.2 < 0.4


def test_card_no_data():
    c = build_card("TCS", None)
    assert c["has_data"] is False and c["clear"] is False and c["bias"] == "neutral"


def test_scan_sorts_clear_first_then_data():
    frames = {
        "AAA": pd.DataFrame([_row("neutral", -0.05)]).set_index("ts"),   # has data, not clear
        "BBB": pd.DataFrame([_row("bearish", -0.7)]).set_index("ts"),    # CLEAR
        "CCC": None,                                                     # no data
    }
    cards = scan(["AAA", "BBB", "CCC"], lambda s: frames.get(s))
    assert [c["symbol"] for c in cards] == ["BBB", "AAA", "CCC"]         # clear → data → none
    assert cards[0]["clear"] and not cards[2]["has_data"]


def test_card_is_nan_safe_and_json_encodable():
    """A first-of-day summary row has NULL buildup columns -> pandas NaN; the card must
    swap NaN for None or FastAPI's allow_nan=False JSON encoding 500s the endpoint
    (the live '⟳ Refresh -> HTTP 500' bug)."""
    import json
    row = {"ts": "t", "spot": float("nan"), "buildup_bias": None,
           "buildup_score": float("nan"), "call_writing": float("nan"),
           "put_writing": None, "call_wall_strike": 24600.0,
           "put_shelf_strike": float("nan"), "res_ext1": 24637.0,
           "sup_ext1": float("nan"), "buildup_call_strike": float("nan"),
           "buildup_put_strike": None, "pcr": float("nan")}
    c = build_card("X", row)
    json.dumps(c, allow_nan=False)                 # must not raise
    assert c["score"] is None and c["strength"] == 0.0 and c["clear"] is False
    assert c["spot"] is None and c["resistance"] == 24600.0
