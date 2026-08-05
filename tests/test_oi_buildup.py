"""OI-buildup engine — ΔOI × Δprice matrix, aggregate lean, reversal levels, boost."""

from __future__ import annotations

import numpy as np
import pandas as pd

from feeds.oi_buildup import (
    buildup_table, buildup_signal, compute_buildup, earliest_snapshot,
    LONG_BUILDUP, SHORT_BUILDUP, SHORT_COVERING, LONG_UNWINDING, FLAT, UNKNOWN)
from feeds.oi_levels import reversal_levels
from analysis.trade1 import oi_confidence_boost, apply_oi_confidence, apply_oi_boost
from analysis.proposal import TradeProposal, Recommendation


def _chain(strikes, coi, poi, cltp, pltp, ts=None):
    d = {"strike": strikes, "call_oi": coi, "put_oi": poi,
         "call_ltp": cltp, "put_ltp": pltp}
    if ts is not None:
        d["ts"] = [ts] * len(strikes)
    return pd.DataFrame(d)


# ---- the 8-cell matrix (call/put × ΔOI sign × Δprice sign) ------------------ #
def test_matrix_all_eight_states():
    s = [100]
    # Each case: (dOI, dPrice) applied to the given side.
    prev = _chain(s, [10], [10], [5], [5])
    # call long buildup: OI+, price+  -> bullish
    t = buildup_table(prev, _chain(s, [12], [10], [6], [5]), 100.0, window=50)
    assert t.iloc[0]["call_state"] == LONG_BUILDUP and t.iloc[0]["call_bias"] == 1
    # call writing (short buildup): OI+, price- -> bearish
    t = buildup_table(prev, _chain(s, [12], [10], [4], [5]), 100.0, window=50)
    assert t.iloc[0]["call_state"] == SHORT_BUILDUP and t.iloc[0]["call_bias"] == -1
    # call short covering: OI-, price+ -> bullish
    t = buildup_table(prev, _chain(s, [8], [10], [6], [5]), 100.0, window=50)
    assert t.iloc[0]["call_state"] == SHORT_COVERING and t.iloc[0]["call_bias"] == 1
    # call long unwinding: OI-, price- -> bearish
    t = buildup_table(prev, _chain(s, [8], [10], [4], [5]), 100.0, window=50)
    assert t.iloc[0]["call_state"] == LONG_UNWINDING and t.iloc[0]["call_bias"] == -1
    # put writing (short buildup): OI+, price- -> bullish (support)
    t = buildup_table(prev, _chain(s, [10], [12], [5], [4]), 100.0, window=50)
    assert t.iloc[0]["put_state"] == SHORT_BUILDUP and t.iloc[0]["put_bias"] == 1
    # put long buildup: OI+, price+ -> bearish
    t = buildup_table(prev, _chain(s, [10], [12], [5], [6]), 100.0, window=50)
    assert t.iloc[0]["put_state"] == LONG_BUILDUP and t.iloc[0]["put_bias"] == -1
    # put short covering: OI-, price+ -> bearish
    t = buildup_table(prev, _chain(s, [10], [8], [5], [6]), 100.0, window=50)
    assert t.iloc[0]["put_state"] == SHORT_COVERING and t.iloc[0]["put_bias"] == -1
    # put long unwinding: OI-, price- -> bullish
    t = buildup_table(prev, _chain(s, [10], [8], [5], [4]), 100.0, window=50)
    assert t.iloc[0]["put_state"] == LONG_UNWINDING and t.iloc[0]["put_bias"] == 1


def test_flat_when_oi_unchanged_or_price_flat():
    prev = _chain([100], [10], [10], [5], [5])
    t = buildup_table(prev, _chain([100], [10], [10], [6], [4]), 100.0, window=50)
    assert t.iloc[0]["call_state"] == FLAT and t.iloc[0]["put_state"] == FLAT  # ΔOI == 0
    t = buildup_table(prev, _chain([100], [12], [12], [5], [5]), 100.0, window=50)
    assert t.iloc[0]["call_state"] == FLAT   # OI moved but price flat -> ambiguous -> flat


def test_unknown_when_price_missing():
    prev = _chain([100], [10], [10], [np.nan], [5])
    t = buildup_table(prev, _chain([100], [12], [10], [np.nan], [5]), 100.0, window=50)
    assert t.iloc[0]["call_state"] == UNKNOWN and t.iloc[0]["call_bias"] == 0


# ---- aggregate signal ------------------------------------------------------- #
def test_signal_bullish_when_puts_written_and_calls_covered():
    strikes = [90, 100, 110]
    prev = _chain(strikes, [10, 10, 10], [10, 10, 10], [5, 5, 5], [5, 5, 5])
    # put writing at 90/100 (OI+, price-) = bullish; call short covering at 110 (OI-, price+) = bullish
    cur = _chain(strikes, [10, 10, 6], [16, 16, 10], [5, 5, 6], [4, 4, 5])
    sig = buildup_signal(buildup_table(prev, cur, 100.0, window=50), 100.0, window=50)
    assert sig["bias"] == "bullish" and sig["score"] > 0 and not sig["insufficient"]
    assert sig["put_writing"] > 0


def test_signal_bearish_when_calls_written():
    strikes = [95, 100, 105]
    prev = _chain(strikes, [10, 10, 10], [10, 10, 10], [5, 5, 5], [5, 5, 5])
    cur = _chain(strikes, [18, 18, 18], [10, 10, 10], [4, 4, 4], [5, 5, 5])  # call writing everywhere
    sig = buildup_signal(buildup_table(prev, cur, 100.0, window=50), 100.0, window=50)
    assert sig["bias"] == "bearish" and sig["score"] < 0
    assert sig["call_writing"] > 0 and sig["dominant_call_strike"] in strikes


def test_signal_insufficient_without_baseline():
    cur = _chain([100], [10], [10], [5], [5])
    assert buildup_signal(buildup_table(None, cur, 100.0), 100.0)["insufficient"]
    assert buildup_table(None, cur, 100.0).empty
    # identical snapshots -> nothing moved -> insufficient
    same = buildup_signal(buildup_table(cur, cur.copy(), 100.0), 100.0)
    assert same["insufficient"]


def test_signal_neutral_when_balanced():
    strikes = [95, 105]
    prev = _chain(strikes, [10, 10], [10, 10], [5, 5], [5, 5])
    # call writing at 105 (bearish) balanced by put writing at 95 (bullish), equal ΔOI
    cur = _chain(strikes, [10, 15], [15, 10], [5, 4], [4, 5], )
    sig = buildup_signal(buildup_table(prev, cur, 100.0, window=50), 100.0, window=50)
    assert sig["bias"] == "neutral" and abs(sig["score"]) <= 0.15


def test_only_overlapping_strikes_and_window():
    prev = _chain([100, 200], [10, 10], [10, 10], [5, 5], [5, 5])
    cur = _chain([100, 300], [12, 10], [10, 10], [6, 5], [5, 5])   # 300 not in prev, 200 not in cur
    t = buildup_table(prev, cur, 100.0, window=1000)
    assert list(t["strike"]) == [100]      # inner join on strike


def test_earliest_snapshot_picks_day_open():
    early = _chain([100], [10], [10], [5], [5], ts="2026-07-29T09:15:00+05:30")
    late = _chain([100], [20], [20], [5], [5], ts="2026-07-29T14:00:00+05:30")
    hist = pd.concat([late, early], ignore_index=True)
    base = earliest_snapshot(hist)
    assert base is not None and base.iloc[0]["call_oi"] == 10   # the 09:15 row
    assert earliest_snapshot(None) is None
    assert earliest_snapshot(pd.DataFrame()) is None


def test_compute_buildup_shape():
    prev = _chain([100], [10], [10], [5], [5])
    out = compute_buildup(prev, _chain([100], [12], [10], [6], [5]), 100.0)
    assert "signal" in out and "table" in out and isinstance(out["table"], list)


# ---- single-snapshot path (Breeze per-strike ΔOI/Δprice fields) ------------- #
def test_direct_change_single_snapshot():
    from feeds.oi_buildup import has_direct_change, buildup_table_from_change
    # a chain carrying its OWN change columns -> no baseline needed
    chain = pd.DataFrame({
        "strike": [100], "call_oi": [12], "put_oi": [14], "call_ltp": [4], "put_ltp": [4],
        "call_oi_chg": [2], "put_oi_chg": [4], "call_ltp_chg": [-1], "put_ltp_chg": [1],
    })
    assert has_direct_change(chain) is True
    t = buildup_table_from_change(chain, 100.0, window=50)
    assert t.iloc[0]["call_state"] == SHORT_BUILDUP     # call OI+, price- = writing (bearish)
    assert t.iloc[0]["put_state"] == LONG_BUILDUP       # put OI+, price+ = long buildup (bearish)
    sig = buildup_signal(t, 100.0, window=50)
    assert sig["bias"] == "bearish" and not sig["insufficient"]


def test_has_direct_change_false_without_fields_or_all_nan():
    plain = _chain([100], [10], [10], [5], [5])
    from feeds.oi_buildup import has_direct_change, buildup_table_from_change
    assert has_direct_change(plain) is False
    assert buildup_table_from_change(plain, 100.0).empty     # falls back to the diff path elsewhere
    nan_cols = plain.assign(call_oi_chg=np.nan, put_oi_chg=np.nan,
                            call_ltp_chg=np.nan, put_ltp_chg=np.nan)
    assert has_direct_change(nan_cols) is False              # present but empty -> not usable


# ---- reversal levels (approx) ---------------------------------------------- #
def test_reversal_levels_off_walls():
    summary = {"call_wall": {"strike": 24200}, "put_shelf": {"strike": 23800}}
    rev = reversal_levels(summary, [37.0, 72.0], None)
    assert rev["source"] == "approx"
    assert rev["bull_reversal"] == 23800 and rev["bear_reversal"] == 24200
    assert rev["bull_reversal_ext"] == 23763.0 and rev["bear_reversal_ext"] == 24237.0
    assert rev["defended"] is None


def test_reversal_defended_follows_writing():
    summary = {"call_wall": {"strike": 24200}, "put_shelf": {"strike": 23800}}
    assert reversal_levels(summary, [37.0], {"put_writing": 500, "call_writing": 100})["defended"] == "support"
    assert reversal_levels(summary, [37.0], {"put_writing": 100, "call_writing": 500})["defended"] == "resistance"


# ---- confidence boost ------------------------------------------------------- #
def _sig(bias, strength):
    return {"bias": bias, "strength": strength, "score": strength if bias == "bullish" else -strength,
            "insufficient": False, "call_writing": 0, "put_writing": 0}


def test_oi_confidence_boost_weighting():
    # strong agree -> +2; plus Claude agreeing caps at +2
    assert oi_confidence_boost("long", _sig("bullish", 0.6), None) == 2
    assert oi_confidence_boost("long", _sig("bullish", 0.6), "bullish") == 2
    # mild agree -> +1
    assert oi_confidence_boost("long", _sig("bullish", 0.2), None) == 1
    # neutral buildup, Claude agrees -> +1 (legacy behaviour)
    assert oi_confidence_boost("long", _sig("neutral", 0.0), "bullish") == 1
    # conflict -> -1
    assert oi_confidence_boost("short", _sig("bullish", 0.6), None) == -1
    # conflict but Claude agrees -> nets to 0
    assert oi_confidence_boost("short", _sig("bullish", 0.6), "bearish") == 0
    # no buildup at all -> Claude-only path
    assert oi_confidence_boost("long", None, "bullish") == 1
    assert oi_confidence_boost("long", None, None) == 0


def test_apply_oi_confidence_sizes_and_caps():
    prop = TradeProposal(instrument="NIFTY", trade_type="trade1", ts="t", direction="long",
                         entry=100.0, stop=95.0, mtf_confidence=4,
                         recommendation=Recommendation.ENTER)
    apply_oi_confidence(prop, _sig("bullish", 0.6), "bullish")
    assert prop.final_confidence == 5          # 4 + 2, capped
    assert prop.oi_confidence_boost == 2
    assert prop.size_lots == 2 and prop.rupee_risk is not None
    assert prop.context["oi_buildup"]["bias"] == "bullish"


def test_apply_oi_boost_back_compat_unchanged():
    prop = TradeProposal(instrument="NIFTY", trade_type="trade1", ts="t", direction="long",
                         entry=100.0, stop=95.0, mtf_confidence=3,
                         recommendation=Recommendation.ENTER)
    apply_oi_boost(prop, "bullish")
    assert prop.oi_confidence_boost == 1 and prop.final_confidence == 4


# ---- moneyness-aware classification (the Sensibull-comparison fix) ---------- #
def test_otm_additions_are_writing_regardless_of_price():
    """The live miscall: on a rising morning OTM call additions (premiums UP) were scored
    'long buildup' (bullish) — they are WRITER-driven resistance and must read bearish."""
    strikes = [24500, 24650]                       # spot 24600: 24650 = OTM call, 24500 = OTM put
    prev = _chain(strikes, [10, 10], [10, 10], [150, 40], [40, 90])
    # up-move: call OI +41L at 24650 with call price UP; put OI +20L at 24500 with put price DOWN
    cur = _chain(strikes, [10, 51], [30, 10], [160, 55], [30, 80])
    t = buildup_table(prev, cur, 24600.0, window=1000)
    r650 = t[t["strike"] == 24650].iloc[0]
    assert r650["call_state"] == SHORT_BUILDUP and r650["call_bias"] == -1   # writing, bearish
    r500 = t[t["strike"] == 24500].iloc[0]
    assert r500["put_state"] == SHORT_BUILDUP and r500["put_bias"] == 1      # writing, bullish
    # and the direct-change path applies the same rule
    from feeds.oi_buildup import buildup_table_from_change
    chain = prev.copy()
    chain["call_oi_chg"] = [0, 41e5]; chain["call_ltp_chg"] = [0, 15]        # price UP
    chain["put_oi_chg"] = [20e5, 0]; chain["put_ltp_chg"] = [-10, 0]
    td = buildup_table_from_change(chain, 24600.0)
    assert td[td["strike"] == 24650].iloc[0]["call_state"] == SHORT_BUILDUP
    assert td[td["strike"] == 24500].iloc[0]["put_state"] == SHORT_BUILDUP


def test_trending_morning_is_lean_not_clear():
    """Put writing below + a call wall being built above (the 5-Aug shape, ~2:1) must read
    as a LEAN, not a pegged CLEAR 1.00 that hides the resistance."""
    strikes = [24400, 24500, 24650, 24700]         # spot 24600
    prev = _chain(strikes, [10, 10, 10, 10], [10, 10, 10, 10],
                  [210, 120, 40, 25], [15, 30, 80, 120])
    cur = _chain(strikes, [10, 10, 51, 56], [110, 172, 10, 10],   # puts +262L below, calls +134L above (10:1... adjust)
                 [220, 130, 55, 35], [10, 22, 70, 110])
    t = buildup_table(prev, cur, 24600.0, window=1000)
    sig = buildup_signal(t, 24600.0, window=1000)
    # put writing 262L bullish vs call writing 87L bearish → positive but NOT clear-strong 1.0
    assert sig["bias"] == "bullish" and 0 < sig["score"] < 1.0
    assert sig["call_writing"] > 0                  # the resistance wall is VISIBLE now
    assert sig["put_writing"] > 0


def test_otm_reductions_are_writers_leaving():
    strikes = [24500, 24700]                        # spot 24600
    prev = _chain(strikes, [10, 50], [40, 10], [150, 30], [30, 120])
    cur = _chain(strikes, [10, 30], [25, 10], [155, 45], [25, 110])   # call wall -20L, put shelf -15L
    t = buildup_table(prev, cur, 24600.0, window=1000)
    r700 = t[t["strike"] == 24700].iloc[0]
    assert r700["call_state"] == SHORT_COVERING and r700["call_bias"] == 1    # resistance lifting
    r500 = t[t["strike"] == 24500].iloc[0]
    assert r500["put_state"] == LONG_UNWINDING and r500["put_bias"] == -1     # support weakening
