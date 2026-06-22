"""Tests for the three new mechanical option strategies (CPR-Supertrend, ORB+VWAP,
expiry Iron Condor) — voters, proposers, the condor math, and the backtest dispatch.

All offline: voters are exercised on directly-built feature frames (deterministic
state-machine cases), proposers on synthetic snapshots, and the backtest rig on a
small synthetic ladder. No network, no Anthropic.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd

from indicators.directional import (
    VOTERS, vote_cpr_supertrend, vote_orb_vwap, vote_iron_condor_regime,
    cpr_st_mtf_config, orb_mtf_config, iron_condor_config,
    resolve_direction_mtf,
)
from analysis.trade1 import build_directional_proposal
from analysis.proposal import Recommendation
from analysis.condor import (
    condor_legs, condor_payoff, expected_move, propose_condor, list_condor_triggers)


# --------------------------------------------------------------------------- #
# Voters
# --------------------------------------------------------------------------- #
def test_new_voters_registered():
    for name in ("cpr_supertrend", "orb_vwap", "iron_condor_regime"):
        assert name in VOTERS


def _cpr_st_feats() -> pd.DataFrame:
    """4 sessions, constant (narrow) CPR width so the narrow-quartile gate opens from
    the 3rd day; the last session carries an uptrend → 5-EMA pullback → reclaim."""
    idx = []
    base = pd.Timestamp("2026-06-15 09:15")          # Mon..Thu
    for d in range(4):
        for b in range(6):
            idx.append(base + pd.Timedelta(days=d, minutes=3 * b))
    idx = pd.DatetimeIndex(idx)
    n = len(idx)
    close = np.full(n, 100.0)
    ema5 = np.full(n, 100.0)
    ema45 = np.full(n, 90.0)
    st = np.ones(n)
    ctc = np.full(n, 95.0)
    cbc = np.full(n, 85.0)
    cw = np.full(n, 0.1)
    # last session (bars n-6..n-1): qualify → pullback → reclaim
    seq = [110, 99, 110, 110, 110, 110]
    for j, b in enumerate(range(n - 6, n)):
        close[b] = seq[j]
    return pd.DataFrame({"close": close, "ema_5": ema5, "ema_45": ema45, "st_dir": st,
                         "cpr_tc": ctc, "cpr_bc": cbc, "cpr_width": cw}, index=idx)


def test_vote_cpr_supertrend_fires_on_pullback_reclaim():
    df = _cpr_st_feats()
    v = vote_cpr_supertrend(df)
    assert set(v.unique()) <= {-1, 0, 1}
    # exactly one long entry, at the reclaim bar (3rd bar of the last session)
    assert int((v > 0).sum()) == 1
    assert v.iloc[-4] == 1


def test_vote_cpr_supertrend_quiet_without_narrow_gate():
    df = _cpr_st_feats()
    df = df.copy()
    df["cpr_width"] = np.linspace(0.1, 5.0, len(df))   # last day is the WIDEST → not narrow
    assert int((vote_cpr_supertrend(df) != 0).sum()) == 0


def test_vote_orb_vwap_one_shot_after_window():
    idx = pd.date_range("2026-06-16 09:15", periods=8, freq="3min")
    close = np.array([100, 100, 100, 101, 105, 106, 107, 108.0])
    orh = np.array([np.nan, np.nan, np.nan, 104, 104, 104, 104, 104.0])
    df = pd.DataFrame({"close": close, "or_high": orh,
                       "or_low": np.full(8, 95.0), "vwap": np.full(8, 100.0),
                       "ema_45": np.full(8, 99.0)}, index=idx)
    v = vote_orb_vwap(df)
    # breaks the OR high at bar 4 (105>104, >vwap, >ema45) and fires ONCE, not again.
    assert int((v > 0).sum()) == 1
    assert v.iloc[4] == 1


def test_vote_iron_condor_regime_gate():
    # 10 bars on a Tuesday (expiry, weekday=1) after 11:00, tight band, inside CPR.
    idx = pd.date_range("2026-06-16 11:00", periods=10, freq="3min")
    df = pd.DataFrame({"close": np.full(10, 100.0), "bb_width": np.full(10, 0.01),
                       "cpr_bc": np.full(10, 95.0), "cpr_tc": np.full(10, 105.0)}, index=idx)
    assert idx[0].weekday() == 1
    g = vote_iron_condor_regime(df, expiry_weekday=1)
    assert (g.iloc[5:] == 1).all()                     # squeeze quantile warmed → gate open
    # NOT an expiry weekday → never fires.
    df_wed = df.set_axis(idx + pd.Timedelta(days=1))
    assert int(vote_iron_condor_regime(df_wed, expiry_weekday=1).sum()) == 0
    # Before 11:00 → never fires.
    df_am = df.set_axis(pd.date_range("2026-06-16 09:30", periods=10, freq="3min"))
    assert int(vote_iron_condor_regime(df_am, expiry_weekday=1).sum()) == 0


# --------------------------------------------------------------------------- #
# Directional proposer core (shared by CPR-ST + ORB)
# --------------------------------------------------------------------------- #
def _read(call: str) -> dict:
    return {"mtf_call": call,
            "regime_45_daily": 1, "supertrend_3m": 1, "mtf_confidence": 3,
            "levels": {"ema_45": 99.0, "supertrend": 98.0, "cpr_pivot": 100.0,
                       "cpr_tc": 110.0, "cpr_bc": 90.0,
                       "session_low": 97.0, "session_high": 103.0}}


def test_build_directional_proposal_enter_long():
    p = build_directional_proposal(
        instrument="NIFTY", ts="2026-06-16T13:00:00", spot=100.0, read=_read("long"),
        oi=None, macro=None, notes=[], trade_type="cpr_st", oi_levels=None)
    assert p.trade_type == "cpr_st"
    assert p.recommendation is Recommendation.ENTER
    assert p.direction == "long" and p.entry == 100.0
    assert p.stop is not None and p.stop < p.entry < p.target          # long levels straddle
    assert "CE (deep-ITM" in p.vehicle


def test_build_directional_proposal_flat_stands_down():
    p = build_directional_proposal(
        instrument="NIFTY", ts="t", spot=100.0, read=_read("flat"),
        oi=None, macro=None, notes=[], trade_type="orb", oi_levels=None)
    assert p.recommendation is Recommendation.STAND_DOWN
    assert p.entry is None


# --------------------------------------------------------------------------- #
# Condor math + proposer
# --------------------------------------------------------------------------- #
def _chain_table(spot=24000, step=50, span=1000) -> pd.DataFrame:
    rows = []
    for k in range(spot - span, spot + span + step, step):
        ext = max(80 - abs(k - spot) * 0.05, 2)        # extrinsic decays with distance
        rows.append({"strike": k, "call_ltp": max(spot - k, 0) + ext,
                     "put_ltp": max(k - spot, 0) + ext})
    return pd.DataFrame(rows)


def test_condor_legs_credit_breakevens_maxloss():
    t = _chain_table()
    assert expected_move(t, 24000) > 0
    legs = condor_legs(24000, t, wing_width=100)
    assert legs["net_credit"] > 0
    assert legs["be_low"] < 24000 < legs["be_high"]    # breakevens straddle spot
    assert abs(legs["max_loss"] - (legs["wing_width"] - legs["net_credit"])) < 1e-6
    # payoff: max profit at the pin, max loss beyond a wing.
    assert condor_payoff(legs, 24000) == legs["net_credit"]
    assert condor_payoff(legs, 30000) == round(legs["net_credit"] - legs["wing_width"], 2)


def _condor_snap(gate_open: bool):
    """A snapshot stub whose 3-min feats open/close the expiry-day condor gate."""
    when = "2026-06-16 11:30" if gate_open else "2026-06-17 11:30"   # Tue vs Wed
    idx = pd.date_range(when, periods=8, freq="3min")
    feats3 = pd.DataFrame({"close": np.full(8, 24000.0), "bb_width": np.full(8, 0.01),
                           "cpr_bc": np.full(8, 23900.0), "cpr_tc": np.full(8, 24100.0)}, index=idx)
    return SimpleNamespace(instrument="NIFTY", ts=idx[-1].isoformat(), spot=24000.0,
                           feats={"3min": feats3}, frames={"3min": feats3},
                           chart_read={}, oi=None, macro=None, notes=[])


def test_propose_condor_enter_on_gate_and_stand_down_off():
    enter = propose_condor(_condor_snap(True), _chain_table(), expiry_weekday=1, wing_width=100)
    assert enter.trade_type == "trade_condor" and enter.direction == "flat"
    assert enter.recommendation is Recommendation.ENTER
    assert enter.context["legs"]["net_credit"] > 0
    off = propose_condor(_condor_snap(False), _chain_table(), expiry_weekday=1)
    assert off.recommendation is Recommendation.STAND_DOWN
    # gate open but no chain → still stands down (can't price the legs)
    nochain = propose_condor(_condor_snap(True), None, expiry_weekday=1)
    assert nochain.recommendation is Recommendation.STAND_DOWN


def test_list_condor_triggers_breach_is_loss():
    # one gated Tuesday; price blows through the upper short → a LOSS
    idx = pd.date_range("2026-06-16 11:00", periods=10, freq="3min")
    feats3 = pd.DataFrame({"close": np.full(10, 24000.0), "bb_width": np.full(10, 0.01),
                           "cpr_bc": np.full(10, 23900.0), "cpr_tc": np.full(10, 24100.0)}, index=idx)
    frame3 = pd.DataFrame({"open": 24000.0, "high": 24010.0, "low": 23990.0,
                           "close": 24000.0, "volume": 100.0}, index=idx)  # non-zero ATR
    frame3.loc[idx[-1], ["high", "close"]] = 24500.0     # spike past the short call
    trigs = list_condor_triggers(feats3, frame3, expiry_weekday=1, em_mult=1.0, wing_width=100)
    assert len(trigs) == 1 and trigs[0]["outcome"] == "loss"
    assert trigs[0]["points"] < 0
