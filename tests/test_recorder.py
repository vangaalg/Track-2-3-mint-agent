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
