"""Analysis layer — Trade-1 proposal + the discipline gate (offline, fake snapshot)."""

from __future__ import annotations

from types import SimpleNamespace

from analysis.trade1 import propose_trade1, size_for_confidence, SIZE_BAND
from analysis.proposal import Recommendation


def _snapshot(call: str, spot: float = 23900.0, oi=None, conf=None):
    read = {
        "mtf_call": call,
        "regime_45_daily": 1 if call == "long" else -1,
        "supertrend_3m": 1 if call == "long" else -1,
        "ema5_trigger_3m": 1 if call == "long" else -1,
        "levels": {"ema_45": 23850.0, "supertrend": 23820.0,
                   "cpr_pivot": 23880.0, "cpr_tc": 23960.0, "cpr_bc": 23800.0},
    }
    if conf is not None:
        read["mtf_confidence"] = conf
    return SimpleNamespace(
        instrument="NIFTY", ts="2024-01-01T15:00:00+05:30", spot=spot,
        chart_read=read, oi=oi, macro=None, notes=[],
    )


def test_flat_read_stands_down():
    prop = propose_trade1(_snapshot("flat"))
    assert prop.recommendation is Recommendation.STAND_DOWN
    assert prop.entry is None and prop.stop is None
    assert "STAND DOWN" in prop.reasons[0]


def test_clean_long_enters_with_valid_levels():
    prop = propose_trade1(_snapshot("long"), size_lots=75)
    assert prop.recommendation is Recommendation.ENTER
    assert prop.stop < prop.entry < prop.target          # long geometry
    assert prop.rr_ratio is not None and prop.rr_ratio > 0
    assert prop.rupee_risk is not None and prop.rupee_risk > 0
    assert prop.vehicle.endswith("CE (deep-ITM, ~0.8-1.0 delta)")
    assert all(prop.checklist[k] for k in prop.checklist)  # six lines filled


def test_clean_short_enters_mirrored():
    prop = propose_trade1(_snapshot("short"), size_lots=75)
    assert prop.recommendation is Recommendation.ENTER
    assert prop.target < prop.entry < prop.stop          # short geometry
    assert " PE " in prop.vehicle


def test_oversize_is_blocked_even_on_clean_read():
    prop = propose_trade1(_snapshot("long"), size_lots=200)
    assert prop.recommendation is Recommendation.STAND_DOWN
    assert "outside the normal" in prop.reasons[-1]


def test_mtf_confidence_scales_size_across_band():
    lo, hi = SIZE_BAND
    assert size_for_confidence(0) == lo and size_for_confidence(5) == hi
    assert lo < size_for_confidence(3) < hi
    # Full HTF agreement -> top of the band; none -> bottom.
    full = propose_trade1(_snapshot("long", conf=5), size_lots=75)
    none = propose_trade1(_snapshot("long", conf=0), size_lots=75)
    assert full.recommendation is Recommendation.ENTER
    assert full.size_lots == hi and full.mtf_confidence == 5
    assert none.size_lots == lo and none.mtf_confidence == 0
    # rupee risk tracks the scaled size (bigger size -> bigger risk).
    assert full.rupee_risk > none.rupee_risk


def test_no_confidence_key_keeps_passed_size():
    prop = propose_trade1(_snapshot("long"), size_lots=75)   # no mtf_confidence in read
    assert prop.size_lots == 75 and prop.mtf_confidence == 0


def test_oi_wall_used_as_target():
    # A call wall just above spot (below the CPR-TC) should become the long target.
    oi = {"call_wall": {"strike": 23930.0, "oi": 99},
          "put_shelf": {"strike": 23700.0, "oi": 99}, "pcr": 1.1}
    prop = propose_trade1(_snapshot("long", oi=oi), size_lots=75)
    assert prop.target == 23930.0
