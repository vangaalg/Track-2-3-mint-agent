"""Forward OI/macro recorder: pure stores, level math, session guard, record_once."""

from __future__ import annotations

import pandas as pd
import pytest

from feeds import macro_store, oi_summary_store, oi_store, recorder
from feeds.oi_levels import wall_levels, scaled_offsets, NIFTY_BANDS


# --- macro_store ----------------------------------------------------------- #
def test_macro_store_append_dedup(tmp_path):
    m1 = {"india_vix": {"price": 13.2, "change_pct": -1.5}, "usd_inr": {"price": 83.1, "change_pct": 0.1}}
    macro_store.append_macro(m1, "2026-06-23T10:00:00+05:30", root=tmp_path)
    # same ts re-written → dedup keep-last; new ts extends
    macro_store.append_macro({"india_vix": {"price": 13.9, "change_pct": 4.0}},
                             "2026-06-23T10:00:00+05:30", root=tmp_path)
    macro_store.append_macro(m1, "2026-06-23T10:15:00+05:30", root=tmp_path)
    df = macro_store.load_macro(root=tmp_path)
    assert len(df) == 2 and df["india_vix_price"].iloc[0] == 13.9      # keep-last
    assert "usd_inr_price" in df.columns
    assert macro_store.load_macro(root=tmp_path / "nope") is None


# --- oi_levels (pure) ------------------------------------------------------ #
def test_wall_levels_nifty_offsets():
    summary = {"call_wall": {"strike": 24000.0, "oi": 9e6},
               "put_shelf": {"strike": 23800.0, "oi": 8e6}}
    lv = wall_levels(summary, NIFTY_BANDS)
    assert lv["resistance_strike"] == 24000.0 and lv["support_strike"] == 23800.0
    assert lv["resistance_ext"] == [24037.0, 24072.0]                 # strike + 37/72
    assert lv["support_ext"] == [23763.0, 23728.0]                    # strike − 37/72


def test_scaled_offsets_bigger_for_banknifty():
    # Bank Nifty ~52k scales 37/72 up proportionally; missing spot → base offsets
    big = scaled_offsets(52000.0)
    assert big[0] > NIFTY_BANDS[0] and big[1] > NIFTY_BANDS[1]
    assert scaled_offsets(0) == NIFTY_BANDS


# --- session guard --------------------------------------------------------- #
def test_in_session_bounds():
    assert recorder.in_session("2026-06-23T10:00:00+05:30")           # Tue, mid-session
    assert not recorder.in_session("2026-06-23T08:00:00+05:30")       # pre-open
    assert not recorder.in_session("2026-06-23T16:00:00+05:30")       # post-close
    assert not recorder.in_session("2026-06-20T10:00:00+05:30")       # Saturday


# --- record_once core ------------------------------------------------------ #
def _chain(atm=24000):
    strikes = [atm - 200, atm - 100, atm, atm + 100, atm + 200]
    return pd.DataFrame({
        "strike": strikes,
        "call_oi": [1e6, 2e6, 3e6, 9e6, 1e6],          # call wall at atm+100
        "put_oi": [1e6, 8e6, 3e6, 2e6, 1e6],           # put shelf at atm-100
        "call_ltp": [210, 120, 60, 25, 8],
        "put_ltp": [8, 25, 60, 120, 210],
    })


def test_record_once_writes_all_artifacts_and_isolates_failures(tmp_path):
    instruments = [
        {"name": "NIFTY", "symbol": "NIFTY", "klass": "index", "band": [37.0, 72.0]},
        {"name": "BOOM", "symbol": "BOOM", "klass": "index", "band": "scale"},
    ]
    fetchers = {
        "NIFTY": lambda s: _chain(24000),
        "BOOM": lambda s: (_ for _ in ()).throw(RuntimeError("breeze down")),
    }
    spot_fns = {"NIFTY": lambda s: 24010.0}
    errors = []
    res = recorder.record_once(instruments, fetchers, spot_fns,
                               macro_fn=lambda: {"india_vix": {"price": 13.0, "change_pct": 1.0}},
                               now="2026-06-23T10:00:00+05:30", root=tmp_path, errors=errors)
    # NIFTY recorded; BOOM failed but did NOT abort the cycle
    assert res["saved"] == ["NIFTY"] and res["macro"] is True
    assert any("BOOM" in e for e in errors)
    # full chain snapshot + summary row + macro row all persisted
    assert oi_store.list_snapshots("NIFTY", base=tmp_path / "oi")
    summ = oi_summary_store.load_summary("NIFTY", root=tmp_path / "oi_summary")
    assert summ is not None and summ["call_wall_strike"].iloc[0] == 24100.0
    assert summ["res_ext1"].iloc[0] == 24137.0                        # wall 24100 + 37
    assert macro_store.load_macro(root=tmp_path / "macro") is not None


def _chain_change(bias="bearish", atm=24000):
    """A chain carrying its OWN ΔOI/Δprice fields -> single-snapshot strong lean."""
    base = _chain(atm)
    if bias == "bearish":                 # call writing across the board -> strong bearish
        base["call_oi_chg"] = [5e5] * 5; base["call_ltp_chg"] = [-5] * 5
        base["put_oi_chg"] = [0] * 5; base["put_ltp_chg"] = [0] * 5
    else:                                 # put writing across the board -> strong bullish
        base["put_oi_chg"] = [5e5] * 5; base["put_ltp_chg"] = [-5] * 5
        base["call_oi_chg"] = [0] * 5; base["call_ltp_chg"] = [0] * 5
    return base


def test_record_once_pushes_clear_lean_once_then_dedups(tmp_path):
    inst = [{"name": "NIFTY", "symbol": "NIFTY", "klass": "index", "band": [37.0, 72.0]}]
    spot_fns = {"NIFTY": lambda s: 24010.0}
    sent, alert_state = [], {}
    notify_fn = lambda text: sent.append(text) or True

    def rec(chain, now="2026-06-23T10:00:00+05:30"):
        recorder.record_once(inst, {"NIFTY": lambda s: chain}, spot_fns,
                             now=now, root=tmp_path, notify_fn=notify_fn, alert_state=alert_state)

    rec(_chain_change("bearish"))                     # crosses into CLEAR bearish -> 1 push
    assert len(sent) == 1 and "CLEAR BEARISH" in sent[0]
    rec(_chain_change("bearish"), now="2026-06-23T10:15:00+05:30")  # still bearish -> no repeat
    assert len(sent) == 1
    rec(_chain_change("bullish"), now="2026-06-23T10:30:00+05:30")  # flip -> 1 more push
    assert len(sent) == 2 and "CLEAR BULLISH" in sent[1]


def test_record_once_no_push_without_notify_fn(tmp_path):
    inst = [{"name": "NIFTY", "symbol": "NIFTY", "klass": "index", "band": [37.0, 72.0]}]
    # default (no notify_fn / alert_state) never alerts and never crashes
    res = recorder.record_once(inst, {"NIFTY": lambda s: _chain_change("bearish")},
                               {"NIFTY": lambda s: 24010.0},
                               now="2026-06-23T10:00:00+05:30", root=tmp_path)
    assert res["saved"] == ["NIFTY"]


def test_record_once_falls_back_to_implied_spot(tmp_path):
    instruments = [{"name": "NIFTY", "symbol": "NIFTY", "klass": "index", "band": [37.0, 72.0]}]
    # no spot_fn → implied_spot from put-call parity (~24000 where call_ltp≈put_ltp)
    res = recorder.record_once(instruments, {"NIFTY": lambda s: _chain(24000)},
                               now="2026-06-23T10:00:00+05:30", root=tmp_path)
    assert res["saved"] == ["NIFTY"]
    summ = oi_summary_store.load_summary("NIFTY", root=tmp_path / "oi_summary")
    assert abs(summ["spot"].iloc[0] - 24000) < 60


def test_select_instruments_subset_and_stocks():
    assert [i["name"] for i in recorder.select_instruments(["NIFTY"])] == ["NIFTY"]
    with_stocks = recorder.select_instruments(with_stocks=True)
    assert any(i["klass"] == "stock" for i in with_stocks)
    assert all(i.get("enabled", True) for i in recorder.select_instruments())  # SENSEX off by default


def test_build_macro_gift_manual_overrides_auto(tmp_path):
    from feeds import context_store
    qf = lambda name: {"price": 1.0, "prev_close": 1.0}
    man = tmp_path / "man"
    context_store.save_context(gift_manual="24100", root=man)
    m = recorder.build_macro(qf, gift_fetch=lambda: {"price": 99.0, "change_pct": 1.0},
                             context_root=man)
    assert m["gift_nifty"]["price"] == 24100.0           # manual wins over auto
    # a root with no manual override → the auto fetch is used
    m2 = recorder.build_macro(qf, gift_fetch=lambda: {"price": 99.0, "change_pct": 1.0},
                              context_root=tmp_path / "auto")
    assert m2["gift_nifty"]["price"] == 99.0


def test_opening_burst_cadence():
    """First 45 min of the session: indices every 5 min, stocks every 15; then 15/60."""
    at = lambda hhmm: pd.Timestamp(f"2026-08-05T{hhmm}:00+05:30")
    assert recorder.cadence_min(at("09:20"), "index") == 5
    assert recorder.cadence_min(at("09:20"), "stock") == 15
    assert recorder.cadence_min(at("09:59"), "stock") == 15
    assert recorder.cadence_min(at("10:00"), "index") == 15   # burst over
    assert recorder.cadence_min(at("10:00"), "stock") == 60
    assert recorder.cadence_min(at("09:20"), "stock", open_burst_min=0) == 60   # burst disabled


def test_due_instruments_uses_burst_window():
    insts = [{"name": "NIFTY", "klass": "index"}, {"name": "RELIANCE", "klass": "stock"}]
    t0 = pd.Timestamp("2026-08-05T09:16:00+05:30")
    last = {"NIFTY": t0, "RELIANCE": t0}
    # 6 minutes later, inside the burst: BOTH... index due (5m passed), stock not yet (15m)
    due1 = recorder.due_instruments(insts, last, t0 + pd.Timedelta(minutes=6))
    assert [i["name"] for i in due1] == ["NIFTY"]
    due2 = recorder.due_instruments(insts, last, t0 + pd.Timedelta(minutes=16))
    assert {i["name"] for i in due2} == {"NIFTY", "RELIANCE"}
    # post-burst, 20 min after a 10:30 pass: index due (15m), stock not (60m)
    t1 = pd.Timestamp("2026-08-05T10:30:00+05:30")
    last2 = {"NIFTY": t1, "RELIANCE": t1}
    due3 = recorder.due_instruments(insts, last2, t1 + pd.Timedelta(minutes=20))
    assert [i["name"] for i in due3] == ["NIFTY"]


def test_record_once_first_pull_uses_prev_close_baseline(tmp_path):
    """The first snapshot of a NEW day diffs against the PREVIOUS session's close —
    the overnight positioning read, available at 09:15 instead of an hour later."""
    inst = [{"name": "NIFTY", "symbol": "NIFTY", "klass": "index", "band": [37.0, 72.0]}]
    spot_fns = {"NIFTY": lambda s: 24010.0}
    prev = _chain(24000)                                   # yesterday's close chain
    recorder.record_once(inst, {"NIFTY": lambda s: prev}, spot_fns,
                         now="2026-08-04T15:25:00+05:30", root=tmp_path)
    cur = _chain(24000)
    cur["call_oi"] = cur["call_oi"] + 5e5                  # overnight call writing...
    cur["call_ltp"] = cur["call_ltp"] - 5                  # ...price down = bearish supply
    recorder.record_once(inst, {"NIFTY": lambda s: cur}, spot_fns,
                         now="2026-08-05T09:16:00+05:30", root=tmp_path)
    summ = oi_summary_store.load_summary("NIFTY", root=tmp_path / "oi_summary")
    row = summ.iloc[-1]                                    # today's 09:16 row HAS a buildup
    assert row["buildup_bias"] == "bearish" and row["buildup_score"] < 0
