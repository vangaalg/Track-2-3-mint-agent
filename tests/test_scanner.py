"""NSE-50 scanner — agreement + Claude/chain gating + error isolation (offline)."""

from __future__ import annotations

import types

import feeds.scanner as sc


class _Snap:
    def __init__(self):
        self.spot = 24010.0
        self.feats, self.frames, self.chart_read, self.oi = {}, {}, {}, None


def _read(rec="enter", bias="bullish", conf=4):
    return types.SimpleNamespace(recommendation=rec, oi_bias=bias, confidence=conf,
                                 proposed_target=None, proposed_stop=None)


_LONG = {"ts": "2026-06-23T13:00:00+05:30", "direction": "long", "entry": 100.0,
         "eng_stop": 99.0, "eng_target": 103.0, "eng_rr": 3.0,
         "mtf_confidence": 4, "outcome": "open"}


def _patch(monkeypatch, trig):
    monkeypatch.setattr(sc, "build_snapshot", lambda *a, **k: _Snap())
    monkeypatch.setattr(sc, "list_triggers", lambda *a, **k: ([trig] if trig else []))
    monkeypatch.setattr(sc, "fetch_oi", lambda *a, **k: {"pcr": 1.1})


def test_highlight_on_full_agreement(monkeypatch):
    _patch(monkeypatch, _LONG)
    calls = []
    row = sc.scan_symbol("RELIANCE", None, None, chain_fn=lambda s: "CH",
                         read_fn=lambda snap, prop: calls.append(1) or _read("enter", "bullish"))
    assert row["highlight"] is True and row["agree"] is True
    assert row["trigger"]["direction"] == "long" and row["claude"]["recommendation"] == "enter"
    assert calls == [1]                       # Claude ran (a trigger was present)


def test_no_highlight_when_oi_or_claude_disagree(monkeypatch):
    _patch(monkeypatch, _LONG)
    # OI bias bearish vs a long trigger -> no agreement
    bias_off = sc.scan_symbol("INFY", None, None, chain_fn=lambda s: "CH",
                              read_fn=lambda snap, prop: _read("enter", "bearish"))
    assert bias_off["highlight"] is False and bias_off["trigger"] is not None
    # Claude stands down -> no agreement (even though OI agrees)
    stand = sc.scan_symbol("INFY", None, None, chain_fn=lambda s: "CH",
                           read_fn=lambda snap, prop: _read("stand_down", "bullish"))
    assert stand["highlight"] is False


def test_no_trigger_skips_chain_and_claude(monkeypatch):
    _patch(monkeypatch, None)                 # no trigger enumerated
    calls = []
    row = sc.scan_symbol("TCS", None, None,
                         chain_fn=lambda s: calls.append("chain") or "CH",
                         read_fn=lambda snap, prop: calls.append("claude") or _read())
    assert row["trigger"] is None and row["highlight"] is False
    assert calls == []                        # gated: neither the chain nor Claude ran


def test_scan_universe_isolates_errors_and_highlights_first(monkeypatch):
    monkeypatch.setattr(sc, "build_snapshot", lambda *a, **k: _Snap())
    monkeypatch.setattr(sc, "fetch_oi", lambda *a, **k: {"pcr": 1.0})
    monkeypatch.setattr(sc, "list_triggers", lambda f, fr, cfg=None, **k: [_LONG])

    def pull(sym):
        if sym == "BAD":
            raise RuntimeError("pull failed")
        return (sym, None)

    rows = sc.scan_universe(["BAD", "RELIANCE"], pull, lambda s: "CH",
                            lambda snap, prop: _read("enter", "bullish"), pace_s=0)
    syms = [r["symbol"] for r in rows]
    assert set(syms) == {"BAD", "RELIANCE"}
    assert rows[0]["symbol"] == "RELIANCE" and rows[0]["highlight"] is True   # highlight first
    bad = next(r for r in rows if r["symbol"] == "BAD")
    assert bad.get("error") and bad["highlight"] is False                     # isolated, no crash


def test_cache_dedups_claude_per_trigger(monkeypatch):
    """A still-open trigger scanned every cycle is Claude-read ONCE (the token-drain fix):
    the second scan of the same (symbol, trigger-ts) reuses the cached read — no new API call."""
    _patch(monkeypatch, _LONG)
    calls = []
    cache = {}
    rf = lambda snap, prop: (calls.append(1), _read("enter", "bullish"))[1]
    r1 = sc.scan_symbol("RELIANCE", None, None, chain_fn=lambda s: "CH", read_fn=rf, cache=cache)
    r2 = sc.scan_symbol("RELIANCE", None, None, chain_fn=lambda s: "CH", read_fn=rf, cache=cache)
    assert calls == [1]                          # Claude ran only on the FIRST scan
    assert r1["highlight"] and r2["highlight"]   # both rows still report the (cached) agreement
    assert r2["claude"]["recommendation"] == "enter" and r2["claude_full"] is not None
    # a DIFFERENT trigger ts is a fresh read
    other = {**_LONG, "ts": "2026-06-23T14:00:00+05:30"}
    _patch(monkeypatch, other)
    sc.scan_symbol("RELIANCE", None, None, chain_fn=lambda s: "CH", read_fn=rf, cache=cache)
    assert calls == [1, 1]                        # one more call for the new trigger


def test_scan_row_carries_full_read(monkeypatch):
    """Each triggered row exposes the FULL Claude read (claude_full) so a stock's analysis is
    readable like an index — not just the verdict chip."""
    _patch(monkeypatch, _LONG)
    row = sc.scan_symbol("INFY", None, None, chain_fn=lambda s: "CH",
                         read_fn=lambda snap, prop: _read("enter", "bullish"))
    assert row["claude_full"]["recommendation"] == "enter"
    assert row["claude_full"]["confidence"] == 4
