"""Order sizing — pure, broker-agnostic."""

from __future__ import annotations

from execution.sizing import qty_for_order, DEFAULT_MAX_AMOUNT


def test_option_qty_is_lots_times_lot_size():
    assert qty_for_order(segment="option", lots=2, lot_size=65) == 130
    assert qty_for_order(segment="option", lots=1, lot_size=30) == 30


def test_equity_qty_floors_max_amount_over_price():
    assert qty_for_order(segment="equity", max_amount=10_000, share_price=2500) == 4
    assert qty_for_order(segment="equity", max_amount=10_000, share_price=2999) == 3   # floor


def test_equity_default_max_amount():
    assert qty_for_order(segment="equity", share_price=1000) == int(DEFAULT_MAX_AMOUNT // 1000)


def test_equity_zero_on_bad_inputs():
    assert qty_for_order(segment="equity", max_amount=10_000, share_price=0) == 0
    assert qty_for_order(segment="equity", max_amount=0, share_price=100) == 0
    assert qty_for_order(segment="equity", max_amount=10_000, share_price=None) == 0


def test_never_negative():
    assert qty_for_order(segment="option", lots=0, lot_size=65) == 0
    assert qty_for_order(segment="equity", max_amount=-1, share_price=100) == 0
