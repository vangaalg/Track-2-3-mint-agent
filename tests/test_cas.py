"""3:15 CAS setup — EP engine, signal thresholds, phases, k calibration, store."""

from __future__ import annotations

import pandas as pd

from analysis.cas import (excess_premium, cas_signal, phase, estimate_k, cas_card,
                          spot_looks_valid)
from feeds.futures import parse_futures_ltp
from journal import store


def test_excess_premium():
    # the verified Aug-5 example: spot froze 24570, futures 24648, carry ~25 → EP ≈ +53
    assert excess_premium(24648.0, 24570.0, 25.0) == 53.0
    assert excess_premium(None, 24570.0) is None
    assert excess_premium(24648.0, None) is None


def test_cas_signal_thresholds():
    assert cas_signal(55.0)["side"] == "CE" and "ITM CE" in cas_signal(55.0)["action"]
    assert cas_signal(-40.0)["side"] == "PE"
    assert cas_signal(10.0)["side"] is None and "NO TRADE" in cas_signal(10.0)["action"]
    assert cas_signal(-14.9)["side"] is None
    assert cas_signal(15.0)["side"] == "CE"          # inclusive at the bar
    assert cas_signal(None)["side"] is None


def test_phase_boundaries():
    at = lambda hms: pd.Timestamp(f"2026-08-05T{hms}+05:30")
    assert phase(at("14:00:00")) == "pre"
    assert phase(at("15:11:00")) == "warmup"
    assert phase(at("15:13:00")) == "decide"
    assert phase(at("15:14:30")) == "enter"
    assert phase(at("15:15:30")) == "burst"
    assert phase(at("15:17:00")) == "flatten"
    assert phase(at("15:30:00")) == "auction"
    assert phase(at("15:45:00")) == "done"


def test_estimate_k_through_origin():
    rows = [{"ep": 55.0, "burst_pts": 25.0},          # the Aug-5 seed (k ≈ 0.45)
            {"ep": -40.0, "burst_pts": -22.0},
            {"ep": 20.0, "burst_pts": None},           # unfinished session ignored
            {"ep": None, "burst_pts": 5.0}]
    k = estimate_k(rows)
    assert k is not None and 0.4 < k < 0.6
    assert estimate_k(rows[:1]) is None                # needs 2+ usable sessions


def test_cas_card_assembly():
    log = [{"ep": 55.0, "burst_pts": 25.0}, {"ep": -40.0, "burst_pts": -20.0}]
    card = cas_card(24570.0, 24648.0, pd.Timestamp("2026-08-05T15:13:10+05:30"),
                    fair_carry=25.0, log_rows=log)
    assert card["ep"] == 53.0 and card["signal"]["side"] == "CE"
    assert card["phase"] == "decide" and card["paper_phase"] is True
    assert card["k"] is not None and card["expected_burst_pts"] is not None
    assert "PAPER-LOG" in card["note"]
    # missing futures leg → honest no-data signal, never a guess
    card2 = cas_card(24570.0, None, pd.Timestamp("2026-08-05T15:13:10+05:30"))
    assert card2["ep"] is None and card2["signal"]["side"] is None


def test_spot_looks_valid():
    assert spot_looks_valid(24570.0, 24648.0) is True        # ~78-pt basis, plausible
    assert spot_looks_valid(14018.0, 24712.0) is False       # the 6-Aug bug: 43% off = impossible
    assert spot_looks_valid(0.0, 24712.0) is False           # non-positive spot
    assert spot_looks_valid(None, 24712.0) is False
    assert spot_looks_valid(24570.0, None) is True           # no futures to cross-check → accept
    # a genuine CAS-window divergence (spot frozen, futures ran) stays well within tolerance
    assert spot_looks_valid(24570.0, 24680.0) is True        # 110 pts ≈ 0.45% < 3%


def test_cas_card_suppresses_a_bad_spot():
    """The 6-Aug garbage: futures 24712 with spot read as 14018 must NOT print a BUY signal —
    EP suppressed, spot flagged suspect, and the reason surfaced."""
    card = cas_card(14018.0, 24712.0, pd.Timestamp("2026-08-06T13:31:00+05:30"))
    assert card["spot_suspect"] is True
    assert card["ep"] is None and card["signal"]["side"] is None
    assert "spot feed looks wrong" in card["signal"]["action"]
    assert card["expected_burst_pts"] is None
    # a valid spot for the same futures reads normally
    ok = cas_card(24667.0, 24712.0, pd.Timestamp("2026-08-06T13:31:00+05:30"))
    assert ok["spot_suspect"] is False and ok["ep"] == 24712.0 - 24667.0 - 27.0


def test_parse_futures_ltp():
    assert parse_futures_ltp({"Success": [{"ltp": "24648.5"}], "Error": None}) == 24648.5
    assert parse_futures_ltp({"Success": [], "Error": None}) is None
    assert parse_futures_ltp({"Success": None, "Error": "boom"}) is None
    assert parse_futures_ltp("garbage") is None


def test_cas_log_store_round_trip(tmp_path):
    db = tmp_path / "journal.db"
    store.save_cas_log("2026-08-05", {"ep": 55.0, "spot": 24570.0, "futures": 24648.0,
                                      "fair_carry": 25.0, "side": "CE"}, path=db)
    rows = store.load_cas_log(path=db)
    assert len(rows) == 1 and rows[0]["ep"] == 55.0 and rows[0]["burst_pts"] is None
    # the burst lands later — upsert on the same date
    store.save_cas_log("2026-08-05", {"ep": 55.0, "spot": 24570.0, "futures": 24648.0,
                                      "fair_carry": 25.0, "side": "CE",
                                      "burst_pts": 25.0, "k": 0.45}, path=db)
    rows = store.load_cas_log(path=db)
    assert len(rows) == 1 and rows[0]["burst_pts"] == 25.0 and rows[0]["k"] == 0.45
    assert store.load_cas_log(path=tmp_path / "none.db") == []
