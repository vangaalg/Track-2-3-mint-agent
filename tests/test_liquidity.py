"""Daily F&O liquidity ranking (traded value = close × volume)."""

from __future__ import annotations

from feeds.liquidity import rank_by_liquidity


def test_ranks_by_traded_value_desc():
    rows = [
        {"symbol": "A", "close": 100.0, "volume": 1000},    # value 100k
        {"symbol": "B", "close": 50.0, "volume": 10000},    # value 500k (most)
        {"symbol": "C", "close": 200.0, "volume": 500},     # value 100k
    ]
    assert rank_by_liquidity(rows, n=3)[0] == "B"           # highest traded value first
    assert rank_by_liquidity(rows, n=1) == ["B"]


def test_missing_value_falls_to_end_alphabetically():
    rows = [
        {"symbol": "Z", "close": 10.0, "volume": 5},        # tiny value but present
        {"symbol": "M", "close": None, "volume": 100},      # no close -> no value
        {"symbol": "A", "volume": None, "close": 100},      # no volume -> no value
    ]
    out = rank_by_liquidity(rows, n=5)
    assert out[0] == "Z"                                    # the only one with a value
    assert out[1:] == ["A", "M"]                            # rest alphabetical, kept


def test_top_n_cap_and_empty():
    rows = [{"symbol": s, "close": 1.0, "volume": i} for i, s in enumerate("EDCBA", 1)]
    assert len(rank_by_liquidity(rows, n=3)) == 3
    assert rank_by_liquidity([], n=20) == []
    assert rank_by_liquidity(rows, n=0) == []
