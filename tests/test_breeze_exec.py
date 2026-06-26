"""Execution layer — order build + the propose-only / dry-run safety gates."""

from __future__ import annotations

import pytest

from analysis.proposal import TradeProposal, Recommendation
from execution import breeze_exec
from journal.log import log_decision


def _enter_proposal() -> TradeProposal:
    return TradeProposal(
        instrument="NIFTY", trade_type="trade1", ts="2024-01-01T15:00:00+05:30",
        direction="long", entry=23900.0, stop=23880.0, target=23960.0,
        size_lots=75, vehicle="NIFTY 23600 CE (deep-ITM, ~0.8-1.0 delta)",
        rupee_risk=112500.0, rr_ratio=3.0, recommendation=Recommendation.ENTER,
        checklist={k: "x" for k in ("edge", "stop", "size", "invalidation",
                                    "target", "time_container")},
    )


def test_build_order_shape():
    order = breeze_exec.build_order(_enter_proposal())
    assert order["stock_code"] == "NIFTY"
    assert order["right"] == "call"
    assert order["strike_price"] == 23600
    assert order["quantity"] == 75 * breeze_exec.LOT_SIZE
    assert order["exchange_code"] == "NFO"


def test_place_rejects_stand_down():
    p = _enter_proposal()
    p.recommendation = Recommendation.STAND_DOWN
    assert breeze_exec.place(p)["status"] == "rejected"


def test_place_dry_run_by_default(monkeypatch):
    called = {"n": 0}

    def fake_place(**kw):
        called["n"] += 1
        return {"ok": True}

    # live=True but no EXECUTION_LIVE env -> still dry-run, broker NOT called.
    monkeypatch.delenv("EXECUTION_LIVE", raising=False)
    res = breeze_exec.place(_enter_proposal(), live=True, place_fn=fake_place)
    assert res["status"] == "dry_run" and called["n"] == 0
    assert res["order"]["strike_price"] == 23600


def test_place_live_requires_all_gates(monkeypatch):
    called = {"n": 0}

    def fake_place(**kw):
        called["n"] += 1
        return {"order_id": "X1"}

    monkeypatch.setenv("EXECUTION_LIVE", "1")
    res = breeze_exec.place(_enter_proposal(), live=True, place_fn=fake_place)
    assert res["status"] == "placed" and called["n"] == 1
    assert res["broker_response"]["order_id"] == "X1"


def test_log_decision_appends_jsonl(tmp_path):
    path = tmp_path / "decisions.jsonl"
    rec = log_decision(_enter_proposal(), "approved", execution={"status": "dry_run"}, path=path)
    assert rec["decision"] == "approved"
    assert path.exists()
    assert path.read_text().count("\n") == 1
    log_decision(_enter_proposal(), "rejected", path=path)
    assert path.read_text().count("\n") == 2


# --- build_orders (neutral Order) + the BreezeBroker SDK mapping ------------- #
def test_build_orders_option_market_and_limit():
    o = breeze_exec.build_orders(_enter_proposal(), segment="option", order_type="market",
                                 lot_size=65, expiry_date="2024-01-25T06:00:00.000Z")
    assert o.segment == "option" and o.exchange == "NFO" and o.action == "buy"
    assert o.right == "call" and o.strike_price == 23600 and o.product == "options"
    assert o.quantity == 75 * 65 and o.price is None          # market → no price
    lim = breeze_exec.build_orders(_enter_proposal(), segment="option", order_type="limit",
                                   limit_price=120.5, lot_size=65)
    assert lim.order_type == "limit" and lim.price == 120.5


def test_build_orders_equity_caps_by_rupees_no_strike():
    o = breeze_exec.build_orders(_enter_proposal(), segment="equity",
                                 max_amount=10_000, share_price=2500.0)
    assert o.segment == "equity" and o.exchange == "NSE" and o.action == "buy"
    assert o.quantity == 4 and o.strike_price is None and o.right is None
    assert o.product == breeze_exec.STOCK_PRODUCT


def test_build_orders_zero_size_raises():
    with pytest.raises(ValueError):
        breeze_exec.build_orders(_enter_proposal(), segment="equity",
                                 max_amount=10_000, share_price=0)


class _FakeBreeze:
    """A stand-in for the breeze_connect client capturing place_order kwargs."""

    def __init__(self):
        self.calls = []

    def place_order(self, **kw):
        self.calls.append(kw)
        return {"Success": {"order_id": "OID9"}, "Status": 200, "Error": None}


def test_breeze_broker_maps_place_order(monkeypatch):
    fake = _FakeBreeze()
    broker = breeze_exec.BreezeBroker(client=fake)
    o = breeze_exec.build_orders(_enter_proposal(), segment="option", lot_size=65,
                                 expiry_date="2024-01-25T06:00:00.000Z")
    res = broker.place_entry(o)
    assert res.status == "placed" and res.broker_order_id == "OID9"
    kw = fake.calls[0]
    assert kw["stock_code"] == "NIFTY" and kw["right"] == "call"
    assert kw["quantity"] == str(75 * 65) and kw["action"] == "buy"
    assert kw["exchange_code"] == "NFO" and kw["product"] == "options"


def test_breeze_broker_parses_rejection():
    class _Rej(_FakeBreeze):
        def place_order(self, **kw):
            return {"Success": None, "Status": 500, "Error": "insufficient funds"}
    broker = breeze_exec.BreezeBroker(client=_Rej())
    o = breeze_exec.build_orders(_enter_proposal(), segment="equity",
                                 max_amount=10_000, share_price=2500.0)
    res = broker.place_exit(o)
    assert res.status == "rejected" and "insufficient" in res.message
