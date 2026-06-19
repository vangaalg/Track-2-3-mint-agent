"""Live macro fetcher — Twelve Data + Breeze routing (mocked, no network)."""

from __future__ import annotations

import sys
import types

import pytest

from feeds.td_macro import make_quote_fn, SCORECARD_SYMBOLS, DEFAULT_SCORECARD
from feeds.macro import fetch_macro


def _install_fake_requests(monkeypatch, payload):
    mod = types.ModuleType("requests")

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return payload

    mod.get = lambda url, params=None, timeout=None: _Resp()
    monkeypatch.setitem(sys.modules, "requests", mod)


def test_td_quote_routing(monkeypatch):
    _install_fake_requests(
        monkeypatch, {"close": "110.0", "previous_close": "100.0"}
    )
    qf = make_quote_fn(api_key="k", client_factory=lambda: None)
    q = qf("crude_wti")                       # routes to Twelve Data
    assert q == {"price": 110.0, "prev_close": 100.0}


def test_breeze_index_routing():
    class FakeClient:
        def get_quotes(self, **kw):
            return {"Success": [{"ltp": "13.4", "previous_close": "13.0"}], "Error": None}

    qf = make_quote_fn(api_key="k", client_factory=FakeClient)
    q = qf("india_vix")                       # routes to Breeze
    assert q["price"] == 13.4 and q["prev_close"] == 13.0


def test_fetch_macro_end_to_end(monkeypatch):
    _install_fake_requests(
        monkeypatch, {"close": "110.0", "previous_close": "100.0"}
    )

    class FakeClient:
        def get_quotes(self, **kw):
            return {"Success": [{"ltp": "13.4", "previous_close": "13.0"}], "Error": None}

    qf = make_quote_fn(api_key="k", client_factory=FakeClient)
    out = fetch_macro(symbols=SCORECARD_SYMBOLS, quote_fn=qf)
    assert out["crude_wti"]["change_pct"] == 10.0
    assert out["india_vix"]["change_pct"] == pytest.approx(3.0769, rel=1e-3)
    assert set(out) == set(DEFAULT_SCORECARD)
