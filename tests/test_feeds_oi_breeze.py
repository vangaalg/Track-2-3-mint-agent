"""Live OI fetcher — chain merge + the Breeze pull (mocked SDK, no network)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from feeds.breeze_oi import merge_chain, nearest_weekly, make_chain_fetcher
from feeds.oi import summarise_chain, chain_table


def test_merge_chain_aligns_calls_and_puts():
    calls = [{"strike_price": "24000", "open_interest": "50"},
             {"strike_price": "24100", "open_interest": "80"}]
    puts = [{"strike_price": "24000", "open_interest": "70"},
            {"strike_price": "23900", "open_interest": "40"}]
    df = merge_chain(calls, puts)
    assert list(df["strike"]) == [23900.0, 24000.0, 24100.0]
    assert list(df["call_oi"]) == [0.0, 50.0, 80.0]   # 23900 has no call -> 0
    assert list(df["put_oi"]) == [40.0, 70.0, 0.0]
    # Flows straight into the OI summary.
    s = summarise_chain(df, spot=24010.0)
    assert s["call_wall"]["strike"] == 24100.0
    assert s["put_shelf"]["strike"] == 24000.0


def test_merge_chain_keeps_ltp():
    calls = [{"strike_price": "24000", "open_interest": "50", "ltp": "120.5"}]
    puts = [{"strike_price": "24000", "open_interest": "70", "ltp": "72.35"}]
    df = merge_chain(calls, puts)
    row = df.loc[df["strike"] == 24000.0].iloc[0]
    assert row["call_ltp"] == 120.5 and row["put_ltp"] == 72.35


def test_chain_table_time_value_window_and_ranks():
    strikes = [float(s) for s in range(22500, 25501, 500)]  # 22500..25500
    df = pd.DataFrame({
        "strike": strikes,
        "call_oi": [10.0 if s != 24000 else 90.0 for s in strikes],  # peak 24000
        "put_oi": [10.0 if s != 23500 else 80.0 for s in strikes],   # peak 23500
        # deep-ITM 23000 call priced at intrinsic+10; ATM-ish 24000 call all extrinsic
        "call_ltp": [(24000 - s) + 10 if s < 24000 else 50.0 for s in strikes],
        "put_ltp": [(s - 24000) + 8 if s > 24000 else 40.0 for s in strikes],
    })
    t = chain_table(df, spot=24000.0, window=1000)
    # Window ±1000 keeps 23000..25000, drops 22500 and 25500.
    assert t["strike"].min() == 23000 and t["strike"].max() == 25000
    # Deep-ITM 23000 call: intrinsic 1000, ltp 1010 -> time value ~10 (only intrinsic+10).
    assert round(float(t.loc[t["strike"] == 23000, "call_extrinsic"].iloc[0]), 2) == 10.0
    # OI ranks within the window: top call wall = 24000, top put shelf = 23500.
    assert float(t.loc[t["strike"] == 24000, "call_oi_rank"].iloc[0]) == 1.0
    assert float(t.loc[t["strike"] == 23500, "put_oi_rank"].iloc[0]) == 1.0
    # Strikes are plain ints (no .000000 in the display).
    assert str(t["strike"].dtype).startswith("int")


def test_summarise_chain_wall_within_atm_window():
    # Near call peak at 24,000; far positional spike at 25,000 must NOT win.
    strikes = [float(s) for s in range(23800, 25001, 50)]
    df = pd.DataFrame({
        "strike": strikes,
        "call_oi": [200.0 if s == 25000 else (90.0 if s == 24000 else 10.0) for s in strikes],
        "put_oi": [80.0 if s == 23900 else 10.0 for s in strikes],
        "call_ltp": [None] * len(strikes),
        "put_ltp": [None] * len(strikes),
    })
    s = summarise_chain(df, spot=24042.0, atm_window=600)
    assert s["call_wall"]["strike"] == 24000.0     # not the far 25,000 spike
    assert s["put_shelf"]["strike"] == 23900.0


def test_nearest_weekly_thursday():
    # 2026-06-16 is a Tuesday -> next Thursday is 2026-06-18.
    assert nearest_weekly(3, today=date(2026, 6, 16)) == date(2026, 6, 18)
    # On the weekday itself -> same day.
    assert nearest_weekly(3, today=date(2026, 6, 18)) == date(2026, 6, 18)


def test_make_chain_fetcher_pulls_both_rights():
    calls = {"Success": [{"strike_price": "24000", "open_interest": "50"}], "Error": None}
    puts = {"Success": [{"strike_price": "24000", "open_interest": "70"}], "Error": None}
    seen = []

    class FakeClient:
        def get_option_chain_quotes(self, right=None, **kw):
            seen.append((right, kw["expiry_date"]))
            return calls if right == "call" else puts

    fetch = make_chain_fetcher(weekday=3, client_factory=FakeClient)
    df = fetch("NIFTY")

    assert {r for r, _ in seen} == {"call", "put"}
    assert df.loc[df["strike"] == 24000.0, "call_oi"].iloc[0] == 50.0
    assert df.loc[df["strike"] == 24000.0, "put_oi"].iloc[0] == 70.0


def test_chain_fetcher_surfaces_breeze_error():
    class FakeClient:
        def get_option_chain_quotes(self, right=None, **kw):
            return {"Success": None, "Error": "Invalid expiry"}

    fetch = make_chain_fetcher(client_factory=FakeClient)
    with pytest.raises(RuntimeError, match="Invalid expiry"):
        fetch("NIFTY")


def test_fetch_oi_captures_error_for_diagnostics():
    # The real Breeze error must land in the errors list, not vanish silently.
    from feeds.oi import fetch_oi

    def boom(_):
        raise RuntimeError("Invalid expiry date")

    errors = []
    assert fetch_oi("NIFTY", 24000.0, fetch_fn=boom, errors=errors) is None
    assert errors == ["oi: Invalid expiry date"]
