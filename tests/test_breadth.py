"""NIFTY-50 breadth + index-contribution compute (pure, offline)."""

from __future__ import annotations

from feeds.breadth import compute_breadth, NIFTY50_WEIGHTS


def _row(sym, pct):
    return {"symbol": sym, "pct_change": pct, "open": 100.0, "high": 101.0,
            "low": 99.0, "close": 100.0 + pct, "volume": 1000}


def test_advance_decline_counts_every_stock():
    rows = [_row("RELIANCE", 1.0), _row("INFY", -0.5), _row("TCS", 0.0),
            _row("UNKNOWNX", 2.0)]                 # unknown weight still counts in A/D
    b = compute_breadth(rows, nifty_spot=24000)
    assert (b["advance"], b["decline"], b["unchanged"], b["total"]) == (2, 1, 1, 4)


def test_contribution_sign_net_points_and_display_order():
    b = compute_breadth([_row("HDFCBANK", 1.0), _row("RELIANCE", -2.0)], nifty_spot=24000)
    by = {r["symbol"]: r for r in b["rows"]}
    assert by["HDFCBANK"]["contribution"] == round(0.13 * 0.01 * 24000, 1)   # +31.2
    assert by["RELIANCE"]["contribution"] < 0
    assert b["net_points"] == round(by["HDFCBANK"]["contribution"] + by["RELIANCE"]["contribution"], 1)
    assert b["rows"][0]["symbol"] == "HDFCBANK"    # biggest positive contribution sorts first


def test_top_n_by_weight_excludes_unweighted():
    rows = [_row(s, 0.5) for s in list(NIFTY50_WEIGHTS)[:25]] + [_row("ZZZ", 5.0)]
    b = compute_breadth(rows, nifty_spot=24000, top_n=20)
    syms = {r["symbol"] for r in b["rows"]}
    assert len(b["rows"]) == 20 and "ZZZ" not in syms and "HDFCBANK" in syms
    assert b["advance"] == 26                       # all 26 (incl. ZZZ) count in A/D


def test_nifty_spot_none_gives_no_contribution():
    b = compute_breadth([_row("HDFCBANK", 1.0)], nifty_spot=None)
    assert b["rows"][0]["contribution"] is None and b["net_points"] is None and b["advance"] == 1


def test_nan_safe_skips_bad_rows():
    rows = [_row("HDFCBANK", 1.0), {"symbol": "INFY", "pct_change": None},
            {"symbol": "TCS"}]                      # missing pct_change → skipped entirely
    b = compute_breadth(rows, nifty_spot=24000)
    assert b["total"] == 1 and b["advance"] == 1
