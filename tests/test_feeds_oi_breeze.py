"""Live OI fetcher — chain merge + the Breeze pull (mocked SDK, no network)."""

from __future__ import annotations

from datetime import date

import pytest

from feeds.breeze_oi import merge_chain, nearest_weekly, make_chain_fetcher
from feeds.oi import summarise_chain


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
