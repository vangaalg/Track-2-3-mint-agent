"""Web cockpit API — FastAPI TestClient with the engine seams mocked (offline)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import web.server as srv
from agent.read import ClaudeRead


def _synth_1m(days: int = 2) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    frames = []
    start = pd.Timestamp("2024-01-01 09:15", tz="Asia/Kolkata")
    for d in range(days):
        idx = pd.date_range(start + pd.Timedelta(days=d), periods=375, freq="1min",
                            tz="Asia/Kolkata")
        p = 24000 + np.cumsum(rng.standard_normal(len(idx)))
        frames.append(pd.DataFrame(
            {"open": p, "high": p + 2, "low": p - 2, "close": p,
             "volume": rng.integers(100, 1000, len(idx))}, index=idx))
    df = pd.concat(frames); df.index.name = "datetime"; return df


def _synth_daily() -> pd.DataFrame:
    rng = np.random.default_rng(1)
    idx = pd.date_range("2023-11-01", periods=80, freq="1D", tz="Asia/Kolkata")
    p = 24000 + np.cumsum(rng.standard_normal(80) * 20)
    df = pd.DataFrame({"open": p, "high": p + 30, "low": p - 30, "close": p,
                       "volume": rng.integers(1000, 5000, 80)}, index=idx)
    df.index.name = "datetime"; return df


def _chain() -> pd.DataFrame:
    strikes = [float(s) for s in range(23000, 25050, 50)]
    return pd.DataFrame({
        "strike": strikes,
        "call_oi": [9_000_000.0 if s == 24000 else 300_000.0 for s in strikes],
        "put_oi": [9_500_000.0 if s == 24000 else 250_000.0 for s in strikes],
        "call_ltp": [max(24000 - s, 0) + 50 for s in strikes],
        "put_ltp": [max(s - 24000, 0) + 50 for s in strikes],
    })


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "JOURNAL_DB", str(tmp_path / "journal.db"))
    monkeypatch.setattr(srv, "PULL_FN", lambda sym: (_synth_1m(), _synth_daily()))
    monkeypatch.setattr(srv, "CHAIN_FN", lambda sym: _chain())
    monkeypatch.setattr(srv, "MACRO_FN", lambda sym: {"usd_inr": {"price": 1, "change_pct": 0.1}})
    monkeypatch.setattr(srv, "READ_COMPLETER", lambda system, user: ClaudeRead(
        agrees_with_engine=True, chart_analysis="ca", oi_analysis="oa",
        where_moving="wm", right_trade="rt", challenge="ch", recommendation="stand_down",
        confidence=4, key_risk="kr"))
    monkeypatch.setattr(srv, "CHAT_COMPLETER", lambda system, history: "sparring reply")
    srv._state.update(snap=None, prop=None, chain=None, snap_at=0.0, oi_at=0.0,
                      read=None, analysed_bar=None, chat=[],
                      queues={}, heads={}, actioned={}, reads={}, exits={}, records={},
                      position={}, read_saved=set(), stored_reads={}, stored_reads_at=0.0)
    return TestClient(srv.app)


def _open_trig(ts="2024-01-01T09:18:00+05:30", direction="long", conf=3):
    return {"tid": 0, "ts": ts, "date": ts[:10], "direction": direction,
            "entry": 24000.0, "stop": 23980.0, "target": 24060.0, "rr": 1.5,
            "mtf_confidence": conf, "size_lots": 104, "outcome": "open",
            "points": 0.0, "rupees": 0.0}


def _seed_heads(monkeypatch, trade1=None, sid="trade1", trigs=None):
    """Force a deterministic per-strategy queue (`sid` gets the triggers, the rest empty)
    so a frozen HEAD exists for the gated-decision tests."""
    empty = {"session": None, "triggers": [], "last": None,
             "summary": {"n": 0, "wins": 0, "losses": 0, "open": 0,
                         "net_points": 0.0, "net_rupees": 0.0, "hit_rate": None}}
    rows = trigs if trigs is not None else (trade1 if trade1 is not None else [_open_trig()])
    q = {"session": "2024-01-01", "triggers": rows, "last": rows[-1] if rows else None,
         "summary": {**empty["summary"], "n": len(rows), "open": len(rows)}}
    monkeypatch.setattr(srv, "_strategy_queue",
                        lambda s, snap, size, session_date=None, lot_size=None:
                        q if s == sid else dict(empty))
    return rows


def test_snapshot_returns_chart_oi_chain_proposal(client):
    d = client.get("/api/snapshot").json()
    assert d["spot"] and d["ts"]
    assert "ema_45" in d["chart"]["numbers"]
    assert d["oi"]["call_wall"]["strike"] == 24000.0     # ATM-window wall
    assert any(r["call_extrinsic"] is not None for r in d["chain"])   # time value present
    assert d["proposal"]["recommendation"] in ("enter", "stand_down")
    assert d["chain"], "per-strike chain rows present"
    # MTF 45-EMA conviction surfaces on the chart block + the proposal.
    conf = d["chart"]["mtf_confidence"]
    assert isinstance(conf, int) and 0 <= conf <= 5
    assert isinstance(d["chart"]["mtf_confidence_breakdown"], dict)
    assert "mtf_confidence" in d["proposal"]
    # Live strike-agent + OI-boost fields are always present on the proposal.
    p = d["proposal"]
    for k in ("selected_strike", "vehicle_extrinsic", "oi_bias",
              "oi_confidence_boost", "final_confidence"):
        assert k in p
    # A directional read picks an ITM vehicle off the live chain.
    if p["direction"] in ("long", "short"):
        assert p["selected_strike"] is not None and p["vehicle_extrinsic"] is not None


def test_snapshot_exposes_all_four_strategy_proposals(client):
    d = client.get("/api/snapshot").json()
    # back-compat: the singular proposal is still Trade-1.
    assert d["proposal"] == d["proposals"]["trade1"]
    # all four strategy streams are present.
    assert set(d["proposals"]) == {"trade1", "cpr_st", "orb", "condor"}
    assert [s["id"] for s in d["strategies"]] == ["trade1", "cpr_st", "orb", "condor"]
    for sid in ("trade1", "cpr_st", "orb"):
        assert d["proposals"][sid]["trade_type"] == sid
        assert d["proposals"][sid]["recommendation"] in ("enter", "stand_down")
    # the condor is non-directional / propose-only.
    assert d["proposals"]["condor"]["trade_type"] == "trade_condor"
    assert d["proposals"]["condor"]["direction"] == "flat"
    # the LIVE proposal build never runs Claude, so it carries no bias until Claude runs
    # on the head (the OI boost lands on the frozen head proposal, not on d["proposals"]).
    assert d["proposals"]["cpr_st"]["oi_bias"] is None


def test_triggers_per_strategy(client):
    client.get("/api/snapshot")
    for sid in ("trade1", "cpr_st", "orb"):
        r = client.get(f"/api/triggers?strategy={sid}").json()
        assert "summary" in r and "triggers" in r
        assert isinstance(r["dates"], list) and r["dates"]          # date toggle options
        assert r["strategy"] == sid and any(s["id"] == "trade1" for s in r["strategies"])
        for t in r["triggers"]:
            assert t["strategy"] == sid and t["strategy_label"]
    cond = client.get("/api/triggers?strategy=condor").json()
    assert "summary" in cond and isinstance(cond["triggers"], list)
    assert client.get("/api/triggers?strategy=bogus").status_code == 404


def test_triggers_all_merges_directional_strategies(client):
    client.get("/api/snapshot")
    d = client.get("/api/triggers?strategy=all").json()
    assert d["strategy"] == "all" and isinstance(d["dates"], list)
    sids = {t["strategy"] for t in d["triggers"]}
    assert "condor" not in sids                       # non-directional has its own tab
    assert sids.issubset({"trade1", "cpr_st", "orb"})
    ts = [t["ts"] for t in d["triggers"]]
    assert ts == sorted(ts)                           # merged rows ordered by time
    for t in d["triggers"]:
        assert t["strategy_label"] and "direction" in t


def test_triggers_date_filter_selects_a_session(client):
    client.get("/api/snapshot")
    dates = client.get("/api/triggers?strategy=all").json()["dates"]
    assert len(dates) >= 1
    d = client.get(f"/api/triggers?strategy=all&date={dates[0]}").json()
    assert d["session"] == dates[0]
    assert all(t["ts"].startswith(dates[0]) for t in d["triggers"])


def test_analyse_returns_four_part_read(client, monkeypatch):
    _seed_heads(monkeypatch)
    d = client.get("/api/snapshot").json()
    # Claude auto-fired once on the new head → cached on the head payload.
    head = d["heads"]["trade1"]
    assert head is not None and head["read"]["chart_analysis"] == "ca"
    assert head["read"]["oi_bias"] == "neutral"
    # the manual re-analyse button hits the same head
    rd = client.post("/api/analyse?strategy=trade1").json()
    assert rd["chart_analysis"] == "ca" and rd["right_trade"] == "rt"
    assert rd["recommendation"] == "stand_down"


def test_head_stable_across_polls(client, monkeypatch):
    _seed_heads(monkeypatch)
    a = client.get("/api/snapshot").json()["heads"]["trade1"]
    b = client.get("/api/snapshot").json()["heads"]["trade1"]
    assert a["ts"] == b["ts"]                      # pinned — no flicker between polls


def test_resolved_trigger_auto_expires_from_head(client, monkeypatch):
    won = _open_trig(); won["outcome"] = "win"     # already resolved
    _seed_heads(monkeypatch, trade1=[won])
    d = client.get("/api/snapshot").json()
    assert d["heads"]["trade1"] is None            # watching — auto-expired out of the head
    assert d["proposals"]["trade1"]["trade_type"] == "trade1"   # tab still renders


def test_approve_acts_on_frozen_trigger_and_advances(client, monkeypatch, tmp_path):
    import journal.log as jlog
    monkeypatch.setattr(srv, "log_decision",
                        lambda p, dec, **k: jlog.log_decision(p, dec, path=tmp_path / "d.jsonl", **k))
    t1 = _open_trig(ts="2024-01-01T09:18:00+05:30")
    t2 = _open_trig(ts="2024-01-01T10:00:00+05:30", direction="short")
    _seed_heads(monkeypatch, trade1=[t1, t2])
    client.get("/api/snapshot")
    r = client.post("/api/decision", data={"action": "approve", "strategy": "trade1",
                                           "ts": t1["ts"], "live": "false"})
    assert r.status_code == 200
    # the logged proposal carries the FROZEN levels of t1
    from journal import store
    rec = store.load_records(srv.JOURNAL_DB)[0]["proposal"]
    assert rec["entry"] == 24000.0 and rec["stop"] == 23980.0 and rec["ts"] == t1["ts"]
    # the decision response advances the card INSTANTLY (no snapshot round-trip needed)
    assert r.json()["next_head"]["ts"] == t2["ts"]
    # next poll advances the head to the second open trigger
    head = client.get("/api/snapshot").json()["heads"]["trade1"]
    assert head["ts"] == t2["ts"]


def test_approve_opposite_auto_flattens_prior_position(client, monkeypatch, tmp_path):
    """One position at a time: approving a trade auto-exits the strategy's prior open trade
    at the live spot (the auto-flatten close is tagged auto=True)."""
    import journal.log as jlog
    from journal import store
    monkeypatch.setattr(srv, "log_decision",
                        lambda p, dec, **k: jlog.log_decision(p, dec, path=tmp_path / "d.jsonl", **k))
    lng = _open_trig(ts="2024-01-01T09:18:00+05:30", direction="long")
    sht = _open_trig(ts="2024-01-01T10:00:00+05:30", direction="short")
    _seed_heads(monkeypatch, trade1=[lng, sht])
    client.get("/api/snapshot")
    client.post("/api/decision", data={"action": "approve", "strategy": "trade1",
                                       "ts": lng["ts"], "live": "false"})
    assert srv._st("NIFTY")["position"]["trade1"]["ts"] == lng["ts"]   # long is the open position
    r = client.post("/api/decision", data={"action": "approve", "strategy": "trade1",
                                           "ts": sht["ts"], "live": "false"})
    assert r.status_code == 200
    af = r.json()["auto_exit"]                          # the prior long was auto-closed
    assert af and af["ts"] == lng["ts"]
    assert ("trade1", lng["ts"]) in srv._st("NIFTY")["exits"]
    long_rec = next(x for x in store.load_records(srv.JOURNAL_DB)
                    if (x.get("proposal") or {}).get("ts") == lng["ts"])
    assert (long_rec.get("outcome") or {}).get("auto") is True
    assert srv._st("NIFTY")["position"]["trade1"]["ts"] == sht["ts"]   # short is now the position


def test_stock_enter_records_under_its_own_symbol(client, monkeypatch, tmp_path):
    """A highlighted scanner stock → POST /api/stock-enter builds the stock's state, records
    the trade under its OWN symbol (so it settles per-instrument), and guards on bad ts."""
    import journal.log as jlog
    from journal import store
    monkeypatch.setattr(srv, "log_decision",
                        lambda p, dec, **k: jlog.log_decision(p, dec, path=tmp_path / "d.jsonl", **k))
    trig = _open_trig(ts="2024-01-02T09:30:00+05:30", direction="long")
    _seed_heads(monkeypatch, trade1=[trig])             # the stock's queue gets this trigger
    r = client.post("/api/stock-enter", data={"symbol": "RELIANCE", "ts": trig["ts"]})
    assert r.status_code == 200
    recs = store.load_records(srv.JOURNAL_DB, symbol="RELIANCE")
    assert recs and recs[-1]["proposal"]["ts"] == trig["ts"]
    assert srv._st("RELIANCE")["position"]["trade1"]["ts"] == trig["ts"]   # now the open position
    # re-entering the same trigger is rejected (already actioned)
    assert client.post("/api/stock-enter",
                       data={"symbol": "RELIANCE", "ts": trig["ts"]}).status_code == 409
    # an unknown ts → 409 (not in the queue, not in the scanner cache)
    assert client.post("/api/stock-enter",
                       data={"symbol": "RELIANCE", "ts": "2024-01-02T11:00:00+05:30"}).status_code == 409


def test_skip_advances_silently_without_logging(client, monkeypatch):
    from journal import store
    t1 = _open_trig(ts="2024-01-01T09:18:00+05:30")
    t2 = _open_trig(ts="2024-01-01T10:00:00+05:30", direction="short")
    _seed_heads(monkeypatch, trade1=[t1, t2])
    client.get("/api/snapshot")
    r = client.post("/api/decision", data={"action": "skip", "strategy": "trade1", "ts": t1["ts"]})
    assert r.status_code == 200 and r.json()["status"] == "skipped"
    # advances to the next trigger…
    assert r.json()["next_head"]["ts"] == t2["ts"]
    assert srv._state["actioned"][("trade1", t1["ts"])] == "skipped"
    # …but records NOTHING (skip is silent — only reject is a logged stand-down)
    assert store.load_records(srv.JOURNAL_DB) == []
    # re-deciding the skipped trigger is a 409 (already actioned)
    r2 = client.post("/api/decision", data={"action": "approve", "strategy": "trade1", "ts": t1["ts"]})
    assert r2.status_code == 409


def test_unknown_action_rejected(client, monkeypatch):
    _seed_heads(monkeypatch)
    client.get("/api/snapshot")
    r = client.post("/api/decision", data={"action": "maybe", "strategy": "trade1"})
    assert r.status_code == 400


def test_stale_ts_rejected(client, monkeypatch):
    _seed_heads(monkeypatch)
    client.get("/api/snapshot")
    r = client.post("/api/decision", data={"action": "approve", "strategy": "trade1",
                                           "ts": "1999-01-01T00:00:00+05:30"})
    assert r.status_code == 409


def test_triggers_lots_scale_by_conviction(client):
    d = client.get("/api/snapshot").json()
    rows = client.get("/api/triggers?strategy=trade1").json()["triggers"]
    from analysis.trade1 import size_for_confidence, LOT_SIZE
    for t in rows:
        assert t["size_lots"] == size_for_confidence(t["mtf_confidence"])
        assert t["rupees"] == round(t["points"] * LOT_SIZE * t["size_lots"], 0)


def test_per_tab_queues_independent(client, monkeypatch):
    _seed_heads(monkeypatch)
    d = client.get("/api/snapshot").json()
    assert d["heads"]["trade1"] is not None
    # the other tabs have their own (empty) queues — unaffected
    assert d["heads"].get("orb") is None and d["heads"].get("cpr_st") is None


def test_oi_boost_auto_applies_on_mechanical_tab(client, monkeypatch, tmp_path):
    """OI confluence is now automatic on the directional mechanical tabs (not just 3-min):
    a CPR-ST long whose chain reads bullish gets its bias + a conviction-nudged size on
    the logged proposal — like Trade-1."""
    import journal.log as jlog
    from journal import store
    monkeypatch.setattr(srv, "log_decision",
                        lambda p, dec, **k: jlog.log_decision(p, dec, path=tmp_path / "d.jsonl", **k))
    monkeypatch.setattr(srv, "READ_COMPLETER", lambda system, user: ClaudeRead(
        agrees_with_engine=True, chart_analysis="ca", oi_analysis="oa", where_moving="wm",
        right_trade="rt", challenge="ch", recommendation="enter", confidence=4,
        key_risk="kr", oi_bias="bullish"))
    t = _open_trig(direction="long", conf=3)
    _seed_heads(monkeypatch, sid="cpr_st", trigs=[t])
    client.get("/api/snapshot")                      # auto-fires Claude on the cpr_st head
    r = client.post("/api/decision", data={"action": "approve", "strategy": "cpr_st",
                                            "ts": t["ts"], "live": "false"})
    assert r.status_code == 200
    from analysis.trade1 import size_for_confidence
    rec = store.load_records(srv.JOURNAL_DB)[0]["proposal"]
    assert rec["oi_bias"] == "bullish"                                  # chain lean recorded
    assert rec["size_lots"] == size_for_confidence(3 + 1)              # +1 conviction nudge


def test_claude_owns_levels_on_live_card(client, monkeypatch, tmp_path):
    """On a 3-min ENTER, Claude's target/stop drive the card + the logged decision (sanity
    rails only — NO 1.5 floor, so R:R can be below 1.5). Engine levels are the fallback."""
    import journal.log as jlog
    from journal import store
    monkeypatch.setattr(srv, "log_decision",
                        lambda p, dec, **k: jlog.log_decision(p, dec, path=tmp_path / "d.jsonl", **k))
    # LONG: Claude picks tighter levels than the engine, R:R 1.0 (would be pushed to 1.5 if floored)
    monkeypatch.setattr(srv, "READ_COMPLETER", lambda system, user: ClaudeRead(
        agrees_with_engine=True, chart_analysis="ca", oi_analysis="oa", where_moving="wm",
        right_trade="rt", challenge="ch", recommendation="enter", confidence=4, key_risk="kr",
        proposed_target=24030.0, proposed_stop=23970.0))   # entry 24000 → reward 30 / risk 30
    t = _open_trig(direction="long", conf=3)               # engine stop 23980 / target 24060
    _seed_heads(monkeypatch, trade1=[t])
    head = client.get("/api/snapshot").json()["heads"]["trade1"]
    assert head["levels_source"] == "claude"
    assert head["stop"] == 23970.0 and head["target"] == 24030.0
    assert head["rr"] == 1.0                                # NOT floored to 1.5
    # the approved/logged trade carries Claude's levels (drives settling/execution)
    r = client.post("/api/decision", data={"action": "approve", "strategy": "trade1",
                                            "ts": t["ts"], "live": "false"})
    assert r.status_code == 200
    rec = store.load_records(srv.JOURNAL_DB)[0]["proposal"]
    assert rec["entry"] == 24000.0 and rec["stop"] == 23970.0 and rec["target"] == 24030.0
    assert rec["rr_ratio"] == 1.0


def test_manual_exit_closes_trigger_and_logs(client, monkeypatch, tmp_path):
    """Exit an OPEN 3-min trigger at a price: the table row flips to 'exit' with realized P&L,
    and a taken+closed trade lands in the journal store. A second exit is a 409."""
    import journal.log as jlog
    from journal import store
    monkeypatch.setattr(srv, "log_decision",
                        lambda p, dec, **k: jlog.log_decision(p, dec, path=tmp_path / "d.jsonl", **k))
    t = _open_trig(direction="long")              # entry 24000
    _seed_heads(monkeypatch, trade1=[t])
    client.get("/api/snapshot")                   # builds the queue
    r = client.post("/api/exit", data={"strategy": "trade1", "ts": t["ts"], "exit_px": "24050"})
    assert r.status_code == 200
    out = r.json()["outcome"]
    assert out["status"] == "win" and out["points"] == 50.0 and out["exit"] == 24050.0
    # the triggers table now shows the row as exited with the realized points
    rows = client.get("/api/triggers?strategy=trade1").json()
    row = next(x for x in rows["triggers"] if x["ts"] == t["ts"])
    assert row["outcome"] == "exit" and row["points"] == 50.0 and row["exit"] == 24050.0
    # a live record was logged + settled with the manual exit
    rec = store.load_records(srv.JOURNAL_DB)[0]
    assert rec["outcome_status"] == "win" and rec["outcome_points"] == 50.0
    assert (rec["outcome"] or {}).get("manual") is True
    # exiting again is rejected
    assert client.post("/api/exit",
                       data={"strategy": "trade1", "ts": t["ts"], "exit_px": "24010"}).status_code == 409


def test_manual_exit_defaults_to_spot(client, monkeypatch, tmp_path):
    """Omitting exit_px closes at the live spot (a short above entry → loss)."""
    import journal.log as jlog
    monkeypatch.setattr(srv, "log_decision",
                        lambda p, dec, **k: jlog.log_decision(p, dec, path=tmp_path / "d.jsonl", **k))
    t = _open_trig(direction="short")             # entry 24000; spot from synth feed > entry → loss
    _seed_heads(monkeypatch, trade1=[t])
    spot = client.get("/api/snapshot").json()["spot"]
    out = client.post("/api/exit", data={"strategy": "trade1", "ts": t["ts"]}).json()["outcome"]
    assert out["exit"] == round(spot, 2)
    assert out["points"] == round(24000.0 - spot, 2)


def test_manual_exit_unknown_trigger_409(client, monkeypatch):
    _seed_heads(monkeypatch)
    client.get("/api/snapshot")
    r = client.post("/api/exit", data={"strategy": "trade1", "ts": "1999-01-01T00:00:00+05:30"})
    assert r.status_code == 409


def test_exit_overrides_a_replay_resolved_row(client, monkeypatch, tmp_path):
    """Exit works on ANY directional row — including one the replay already marked win/loss —
    recording the trade you actually took and overriding that row's hypothetical outcome."""
    import journal.log as jlog
    monkeypatch.setattr(srv, "log_decision",
                        lambda p, dec, **k: jlog.log_decision(p, dec, path=tmp_path / "d.jsonl", **k))
    won = {**_open_trig(direction="long"), "outcome": "win", "points": 60.0, "rupees": 3900.0}
    _seed_heads(monkeypatch, trade1=[won])
    client.get("/api/snapshot")
    r = client.post("/api/exit", data={"strategy": "trade1", "ts": won["ts"], "exit_px": "24010"})
    assert r.status_code == 200 and r.json()["outcome"]["points"] == 10.0   # the REAL exit, not +60
    row = next(x for x in client.get("/api/triggers?strategy=trade1").json()["triggers"]
               if x["ts"] == won["ts"])
    assert row["outcome"] == "exit" and row["points"] == 10.0


def test_exit_reconstructs_from_store_after_restart(client, monkeypatch, tmp_path):
    """Manual exits are durable: after a redeploy wipes the in-memory overlay, the exit is
    rebuilt from the store so the row still shows `exit`."""
    import journal.log as jlog
    monkeypatch.setattr(srv, "log_decision",
                        lambda p, dec, **k: jlog.log_decision(p, dec, path=tmp_path / "d.jsonl", **k))
    t = _open_trig(direction="long")
    _seed_heads(monkeypatch, trade1=[t])
    client.get("/api/snapshot")
    client.post("/api/exit", data={"strategy": "trade1", "ts": t["ts"], "exit_px": "24050"})
    st = srv._st("NIFTY")
    st["exits"].clear(); st["records"].clear()       # simulate a restart (overlay wiped)
    srv._load_persisted_exits("NIFTY")
    key = ("trade1", t["ts"])
    assert key in st["exits"] and st["exits"][key]["points"] == 50.0


def test_state_isolated_per_instrument(client, monkeypatch, tmp_path):
    import journal.log as jlog
    monkeypatch.setattr(srv, "log_decision",
                        lambda p, dec, **k: jlog.log_decision(p, dec, path=tmp_path / "d.jsonl", **k))
    t = _open_trig(direction="long")
    _seed_heads(monkeypatch, trade1=[t])
    client.get("/api/snapshot")
    client.post("/api/exit", data={"strategy": "trade1", "ts": t["ts"],
                                   "exit_px": "24050", "symbol": "NIFTY"})
    key = ("trade1", t["ts"])
    assert key in srv._st("NIFTY")["exits"]
    assert key not in srv._st("BANKNIFTY")["exits"]   # Bank Nifty state is separate


def test_snapshot_exposes_instruments_and_active_symbol(client):
    d = client.get("/api/snapshot").json()
    assert d["symbol"] == "NIFTY"
    assert any(i["id"] == "BANKNIFTY" for i in d["instruments"])


def test_session_dates_newest_first(client):
    client.get("/api/snapshot")
    dates = client.get("/api/triggers?strategy=trade1").json()["dates"]
    assert dates == sorted(dates, reverse=True)


def test_session_dates_prepends_today_on_weekday(monkeypatch):
    """The date toggle includes TODAY (IST) on a weekday even when the frame has no bars yet
    (pre-market), so it defaults to today instead of silently showing yesterday."""
    import web.server as server
    from types import SimpleNamespace
    from datetime import datetime as real_dt, timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    idx = pd.date_range("2024-01-02 09:15", periods=10, freq="3min", tz="Asia/Kolkata")
    snap = SimpleNamespace(frames={"3min": pd.DataFrame({"close": range(10)}, index=idx)})

    class _Mon:
        @staticmethod
        def now(tz=None): return real_dt(2026, 6, 22, 10, 0, tzinfo=ist)   # a Monday
    monkeypatch.setattr(server, "datetime", _Mon)
    dates = server._session_dates(snap)
    assert dates[0] == "2026-06-22"                 # today prepended, newest-first
    assert "2024-01-02" in dates                    # frame's own session still listed

    class _Sat:
        @staticmethod
        def now(tz=None): return real_dt(2026, 6, 20, 10, 0, tzinfo=ist)   # a Saturday
    monkeypatch.setattr(server, "datetime", _Sat)
    assert "2026-06-20" not in server._session_dates(snap)   # no empty weekend "today"


def _seed_oi_history(root, symbol="NIFTY"):
    from feeds import oi_summary_store
    rows = [  # two recorded sessions, a couple of cycles each
        ("2026-06-22T10:00:00+05:30", 1.05, 24050.0, 24100.0, 23900.0),
        ("2026-06-22T13:00:00+05:30", 0.92, 24050.0, 24150.0, 23950.0),
        ("2026-06-23T10:00:00+05:30", 1.20, 24000.0, 24050.0, 23850.0),
    ]
    for ts, pcr, mp, cw, ps in rows:
        oi_summary_store.append_summary(
            symbol, ts, 24010.0,
            {"pcr": pcr, "max_pain": mp, "atm": 24000.0,
             "call_wall": {"strike": cw, "oi": 9e6}, "put_shelf": {"strike": ps, "oi": 8e6}},
            {"resistance_ext": [cw + 37, cw + 72], "support_ext": [ps - 37, ps - 72]},
            root=root)


def test_oi_history_serves_pcr_timeseries(client, tmp_path, monkeypatch):
    """The recorder's PCR/max-pain/walls/bands series is served for the line graph + table,
    newest-day-first, filterable by session and scoped per instrument."""
    root = tmp_path / "oi_summary"
    _seed_oi_history(root, "NIFTY")
    monkeypatch.setattr(srv, "OI_SUMMARY_ROOT", str(root))
    d = client.get("/api/oi-history?symbol=NIFTY").json()
    assert d["days"] == ["2026-06-23", "2026-06-22"]          # newest-first
    assert len(d["rows"]) == 3
    r0 = d["rows"][0]
    assert r0["pcr"] == 1.05 and r0["max_pain"] == 24050.0 and r0["call_wall_strike"] == 24100.0
    assert r0["res_ext1"] == 24137.0 and r0["sup_ext1"] == 23863.0
    # filter to one session
    one = client.get("/api/oi-history?symbol=NIFTY&day=2026-06-23").json()
    assert len(one["rows"]) == 1 and one["rows"][0]["pcr"] == 1.20


def test_oi_history_empty_when_unrecorded(client, tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "OI_SUMMARY_ROOT", str(tmp_path / "none"))
    d = client.get("/api/oi-history?symbol=BANKNIFTY").json()
    assert d["rows"] == [] and d["days"] == []                # nothing recorded yet → honest empty


def test_oi_history_per_instrument(client, tmp_path, monkeypatch):
    root = tmp_path / "oi_summary"
    _seed_oi_history(root, "BANKNIFTY")                       # only Bank Nifty has history
    monkeypatch.setattr(srv, "OI_SUMMARY_ROOT", str(root))
    assert client.get("/api/oi-history?symbol=NIFTY").json()["rows"] == []
    assert len(client.get("/api/oi-history?symbol=BANKNIFTY").json()["rows"]) == 3


def _seed_oi_chains(base, symbol="NIFTY"):
    from feeds import oi_store
    for ts in ("2026-06-22T10:00:00+05:30", "2026-06-23T10:00:00+05:30",
               "2026-06-23T13:00:00+05:30"):
        chain = pd.DataFrame({"strike": [23900.0, 24000.0, 24100.0],
                              "call_oi": [1e6, 2e6, 3e6], "put_oi": [3e6, 2e6, 1e6],
                              "call_ltp": [200.0, 120.0, 60.0], "put_ltp": [50.0, 110.0, 210.0]})
        oi_store.save_chain(symbol, ts, 24010.0, chain, base=base)


def test_oi_download_summary_csv(client, tmp_path, monkeypatch):
    """The PCR/OI summary downloads as a CSV (Excel), date-wise, with a named attachment."""
    root = tmp_path / "oi_summary"
    _seed_oi_history(root, "NIFTY")
    monkeypatch.setattr(srv, "OI_SUMMARY_ROOT", str(root))
    r = client.get("/api/oi-download?symbol=NIFTY&day=2026-06-23&kind=summary")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'filename="NIFTY_oi_summary_2026-06-23.csv"' in r.headers["content-disposition"]
    body = r.text.splitlines()
    assert "pcr" in body[0] and "max_pain" in body[0]            # header row
    assert len(body) == 2 and "1.2" in body[1]                  # only that day's single cycle


def test_oi_download_chain_csv(client, tmp_path, monkeypatch):
    """The full per-strike chain snapshots download as one CSV, filtered by day."""
    base = tmp_path / "oi"
    _seed_oi_chains(base, "NIFTY")
    monkeypatch.setattr(srv, "OI_ROOT", str(base))
    r = client.get("/api/oi-download?symbol=NIFTY&day=2026-06-23&kind=chain")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'filename="NIFTY_chain_2026-06-23.csv"' in r.headers["content-disposition"]
    body = r.text.splitlines()
    assert body[0].startswith("ts,spot,strike,call_oi,put_oi")  # ordered columns
    assert len(body) == 1 + 2 * 3                                # 2 cycles that day x 3 strikes
    # all days = both sessions (3 cycles total x 3 strikes)
    allr = client.get("/api/oi-download?symbol=NIFTY&kind=chain").text.splitlines()
    assert len(allr) == 1 + 3 * 3


def test_oi_download_empty_is_blank_csv(client, tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "OI_SUMMARY_ROOT", str(tmp_path / "none"))
    r = client.get("/api/oi-download?symbol=BANKNIFTY&kind=summary")
    assert r.status_code == 200 and r.text == ""                # no 500, just an empty file


def test_scanner_serves_cache_highlights_first(client):
    srv._SCAN.update(at=123.0, scanning=False, error=None, rows=[
        {"symbol": "RELIANCE", "trigger": {"direction": "long"}, "highlight": True},
        {"symbol": "INFY", "trigger": {"direction": "long"}, "highlight": False},
    ])  # scan_universe already sorts highlights-first; the endpoint serves as-cached
    d = client.get("/api/scanner").json()
    assert d["count"] == 2 and d["highlights"] == 1 and d["triggers"] == 2
    assert d["rows"][0]["symbol"] == "RELIANCE"                 # highlighted stock surfaced first


def test_scanner_refresh_runs_inline_scan(client, monkeypatch):
    srv._SCAN.update(at=0.0, rows=[], scanning=False, error=None)
    seen = {}

    def fake_scan(syms, pull_fn, chain_fn, read_fn, cfg=None, pace_s=0.0, **k):
        seen["n"] = len(syms)                                   # the full stock universe
        return [{"symbol": "RELIANCE", "trigger": {"direction": "long"}, "highlight": True}]

    monkeypatch.setattr(srv.scanner, "scan_universe", fake_scan)
    d = client.post("/api/scanner/refresh").json()
    assert d["count"] == 1 and d["highlights"] == 1
    assert seen["n"] == len(srv.scanner_symbols())             # scanned all 50
    assert srv._SCAN["rows"][0]["symbol"] == "RELIANCE"        # cache populated


def test_cockpit_loads_a_scanner_stock(client):
    """Click-to-focus: the cockpit resolves a registered stock symbol like any instrument."""
    d = client.get("/api/snapshot?symbol=RELIANCE").json()
    assert d["symbol"] == "RELIANCE" and d["spot"]


def test_record_scoped_per_instrument(client, tmp_path, monkeypatch):
    from journal import store
    for s, pts in [("NIFTY", 60.0), ("BANKNIFTY", 40.0)]:
        rid = store.save_decision({
            "decision": "approved", "symbol": s, "ts": "2024-01-01T09:18:00+05:30",
            "proposal": {"recommendation": "enter", "direction": "long", "entry": 100.0,
                         "stop": 100.0, "target": 100.0, "ts": "2024-01-01T09:18:00+05:30"}},
            path=srv.JOURNAL_DB)
        store.update_outcome(rid, {"status": "win", "points": pts, "rupees": 1.0, "manual": True},
                             "good", "deserved", path=srv.JOURNAL_DB)
    client.get("/api/snapshot")
    d = client.get("/api/record?symbol=BANKNIFTY").json()
    assert d["summary"]["n_settled"] == 1                       # only the Bank Nifty trade
    assert d["recent"][0]["outcome"]["points"] == 40.0


def test_chat_round_trips(client):
    client.get("/api/snapshot")
    r = client.post("/api/chat", data={"text": "why flat?"})
    assert r.json()["reply"] == "sparring reply"
    assert srv._state["chat"][0]["content"] == "why flat?"


def test_decision_logs(client, tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "DEFAULT_LOG", str(tmp_path / "d.jsonl"))
    # log_decision is imported into web.server; patch where it's used.
    import journal.log as jlog
    monkeypatch.setattr(srv, "log_decision",
                        lambda p, dec, **k: jlog.log_decision(p, dec, path=tmp_path / "d.jsonl", **k))
    _seed_heads(monkeypatch)
    client.get("/api/snapshot")
    d = client.post("/api/decision", data={"action": "reject"}).json()   # ts omitted → trade1 head
    assert d["logged"] == "rejected"
    assert (tmp_path / "d.jsonl").exists()


@pytest.mark.parametrize("tf", ["1min", "3min", "15min", "60min", "1day", "1week"])
def test_chart_endpoint_per_timeframe(client, tf):
    client.get("/api/snapshot")
    d = client.get(f"/api/chart?tf={tf}&bars=50").json()
    assert d["tf"] == tf and d["bars"]
    assert d["symbol"] == "NIFTY"        # lets the frontend redraw on an instrument switch
    row = d["bars"][-1]
    assert {"o", "h", "l", "c", "ema45", "bb_u", "macd", "rsi", "st"} <= set(row)
    # CPR is sourced from the daily frame, so pivot/tc/bc are always NUMERIC (not None).
    for k in ("pivot", "tc", "bc"):
        assert isinstance(d["cpr"][k], (int, float))


def test_chart_cpr_present_with_single_session_intraday(client, monkeypatch):
    # A live 1-min frame holding ONLY today's session has no prior session, so the
    # intraday CPR would be NaN; the daily-sourced CPR must still populate the lines.
    monkeypatch.setattr(srv, "PULL_FN", lambda sym: (_synth_1m(days=1), _synth_daily()))
    srv._state.update(snap=None, snap_at=0.0)
    client.get("/api/snapshot")
    cpr = client.get("/api/chart?tf=3min&bars=50").json()["cpr"]
    assert isinstance(cpr["pivot"], (int, float)) and isinstance(cpr["bc"], (int, float))


def test_chart_endpoint_unknown_tf_404(client):
    client.get("/api/snapshot")
    assert client.get("/api/chart?tf=nope").status_code == 404


def test_analyse_without_snapshot_409(client):
    assert client.post("/api/analyse").status_code == 409


def test_triggers_endpoint_shape(client):
    client.get("/api/snapshot")
    d = client.get("/api/triggers").json()
    assert set(d) >= {"session", "triggers", "last", "summary"}
    assert set(d["summary"]) >= {"n", "wins", "losses", "open", "net_points", "net_rupees"}
    assert isinstance(d["triggers"], list)


def test_record_endpoint_summarises_from_store(client, tmp_path, monkeypatch):
    """The 2x2 track record is summarised from the durable STORE (where manual exits + the
    settled outcomes land) — not the ephemeral JSONL log, which reads 0 on Railway."""
    from journal import store
    rid = store.save_decision({
        "decision": "approved", "symbol": "NIFTY", "ts": "2024-01-01T09:18:00+05:30",
        "proposal": {"recommendation": "enter", "direction": "long", "entry": 24000.0,
                     "stop": 23980.0, "target": 24060.0, "size_lots": 75,
                     "mtf_confidence": 3, "final_confidence": 4,
                     "ts": "2024-01-01T09:18:00+05:30"},
        "claude_read": {"confidence": 5}}, path=srv.JOURNAL_DB)
    store.update_outcome(rid, {"status": "win", "points": 60.0, "rupees": 4500.0, "manual": True},
                         "good", "deserved", path=srv.JOURNAL_DB)
    client.get("/api/snapshot")
    d = client.get("/api/record").json()
    assert d["summary"]["n_settled"] == 1
    assert d["summary"]["cells"].get("deserved") == 1
    assert d["recent"][0]["process"] == "good"
    assert d["recent"][0]["outcome"]["status"] == "win"
    # both confidence numbers surface for analysis: engine conviction + Claude's
    assert d["recent"][0]["conviction"] == 4 and d["recent"][0]["confidence"] == 5
    # win-rate-by-conviction aggregate: this win lands in the conviction-4 bucket
    b4 = next(b for b in d["by_conviction"] if b["conviction"] == 4)
    assert b4["n"] == 1 and b4["wins"] == 1 and b4["net_points"] == 60.0


def test_decision_persists_full_context(client, tmp_path, monkeypatch):
    import journal.log as jlog
    from journal import store
    monkeypatch.setattr(srv, "DEFAULT_LOG", str(tmp_path / "d.jsonl"))
    monkeypatch.setattr(srv, "log_decision",
                        lambda p, dec, **k: jlog.log_decision(p, dec, path=tmp_path / "d.jsonl", **k))
    _seed_heads(monkeypatch)
    client.get("/api/snapshot")
    client.post("/api/analyse?strategy=trade1")          # populate Claude's read
    client.post("/api/chat", data={"text": "why flat?"})  # populate chat
    client.post("/api/decision", data={"action": "reject"})
    rows = store.load_records(srv.JOURNAL_DB)
    assert len(rows) == 1
    r = rows[0]
    assert r["decision"] == "rejected"
    assert r["claude_read"]["chart_analysis"] == "ca"      # full Claude read saved
    assert any((m.get("content") == "why flat?") for m in r["chat"])  # transcript saved
    assert "3min" in r["chart"] and r["chart"]["3min"]["bars"]        # chart datapoints
    assert r["chain"] and r["chain"][0]["strike"] is not None         # raw chain
    assert r["macro"]["usd_inr"]["price"] == 1                        # macro values


def test_live_decision_captures_trigger_label(client, tmp_path, monkeypatch):
    """A live decision records the trader's genuine/false trigger label; settling the
    track record (/api/record) runs the post-mortem path without error."""
    from journal import store
    from agent.reason import ReasonWhy
    monkeypatch.setattr(srv, "DEFAULT_LOG", str(tmp_path / "d.jsonl"))
    monkeypatch.setattr(srv, "REASON_COMPLETER", lambda system, user: ReasonWhy(
        why="ran straight to target", trigger_quality="false", lesson="skip the graze"))
    _seed_heads(monkeypatch)
    client.get("/api/snapshot")
    client.post("/api/decision", data={"action": "reject", "label": "false"})
    r = store.load_records(srv.JOURNAL_DB)[0]
    assert r["trigger_label"] == "false" and r["reason_why"] is None   # reject -> no outcome yet
    assert client.get("/api/record").status_code == 200               # settle path is clean


def test_triggers_table_exposes_read_actioned_and_pending(client, monkeypatch):
    """Each trigger row carries Claude's auto-read + actioned status; ``pending`` counts the
    undecided — so the table is the persistent place to act on a trigger missed live."""
    t1 = _open_trig(ts="2024-01-01T09:18:00+05:30")
    t2 = _open_trig(ts="2024-01-01T10:00:00+05:30", direction="short")
    _seed_heads(monkeypatch, trade1=[t1, t2])
    client.get("/api/snapshot")                       # head (t1) auto-read by the mocked completer
    d = client.get("/api/triggers?strategy=trade1").json()
    assert d["pending"] == 2                           # neither decided yet
    rows = {r["ts"]: r for r in d["triggers"]}
    assert rows[t1["ts"]]["read"]["recommendation"] == "stand_down"   # auto-read cached on the head
    assert rows[t1["ts"]]["read"]["confidence"] == 4
    assert rows[t2["ts"]]["read"] is None              # not yet the head → no read
    assert rows[t1["ts"]]["actioned"] is None
    # the full read backs the 💬 Discuss panel (params= encodes the +05:30 ts correctly)
    assert client.get("/api/trigger-read",
                      params={"strategy": "trade1", "ts": t1["ts"]}).status_code == 200
    assert client.get("/api/trigger-read",
                      params={"strategy": "trade1", "ts": "nope"}).status_code == 404


def test_decision_acts_on_non_head_trigger_by_ts(client, monkeypatch, tmp_path):
    """Approve/reject ANY trigger row by ts — not just the live head — and it's logged, while
    the live head/position is left untouched (a back-decision on a missed trigger)."""
    import journal.log as jlog
    from journal import store
    monkeypatch.setattr(srv, "log_decision",
                        lambda p, dec, **k: jlog.log_decision(p, dec, path=tmp_path / "d.jsonl", **k))
    t1 = _open_trig(ts="2024-01-01T09:18:00+05:30")
    t2 = _open_trig(ts="2024-01-01T10:00:00+05:30", direction="short")
    _seed_heads(monkeypatch, trade1=[t1, t2])
    client.get("/api/snapshot")
    r = client.post("/api/decision",                  # act on t2 — the NON-head row — by ts
                    data={"action": "reject", "strategy": "trade1", "ts": t2["ts"]})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    st = srv._st("NIFTY")
    assert st["actioned"][("trade1", t2["ts"])] == "rejected"
    assert ("trade1", t1["ts"]) not in st["actioned"]            # the head is untouched
    head = client.get("/api/snapshot").json()["heads"]["trade1"]
    assert head["ts"] == t1["ts"]                                # live head still t1
    assert any(rec["proposal"]["ts"] == t2["ts"]                 # t2's frozen levels were logged
               for rec in store.load_records(srv.JOURNAL_DB))


def test_pending_inbox_holds_undecided_triggers(client, monkeypatch):
    """The live inbox lists today's undecided directional triggers (each with its read) and
    drops a row as soon as it's actioned — so a trigger missed live is held until decided."""
    monkeypatch.setattr(srv, "instrument_list",          # scope to one index for this test
                        lambda: [{"id": "NIFTY", "label": "NIFTY"}])
    monkeypatch.setattr(srv, "_SCAN", {"rows": [], "at": 0.0})   # no scanner stocks here
    t1 = _open_trig(ts="2024-01-01T09:18:00+05:30")
    t2 = _open_trig(ts="2024-01-01T10:00:00+05:30", direction="short")
    _seed_heads(monkeypatch, trade1=[t1, t2])
    client.get("/api/snapshot")
    d = client.get("/api/pending").json()
    assert d["count"] == 2 and {r["ts"] for r in d["rows"]} == {t1["ts"], t2["ts"]}
    assert all(r["symbol"] == "NIFTY" and r["kind"] == "index" for r in d["rows"])
    head_row = next(r for r in d["rows"] if r["ts"] == t1["ts"])
    assert head_row["read"]["recommendation"] == "stand_down"   # the head's auto-read rides along
    client.post("/api/decision", data={"action": "skip", "strategy": "trade1", "ts": t2["ts"]})
    d2 = client.get("/api/pending").json()
    assert d2["count"] == 1 and d2["rows"][0]["ts"] == t1["ts"]  # actioned row left the inbox


def test_pending_inbox_is_cross_instrument(client, monkeypatch):
    """The inbox aggregates BOTH indices (NIFTY + Bank Nifty) AND the scanner's highlighted
    stocks — anything to act on anywhere — so a Bank Nifty / stock trigger isn't missed while
    watching NIFTY. A Bank-Nifty row decides on its OWN instrument."""
    t1 = _open_trig(ts="2024-01-01T09:18:00+05:30")
    _seed_heads(monkeypatch, trade1=[t1])               # same mocked queue for every index symbol
    monkeypatch.setattr(srv, "_SCAN", {"at": 1.0, "rows": [{   # one highlighted scanner stock
        "symbol": "ACME", "spot": 1234.5, "highlight": True, "pcr": 0.9, "oi_bias": "bullish",
        "trigger": {"direction": "long", "entry": 1230.0, "stop": 1220.0, "target": 1250.0,
                    "rr": 2.0, "mtf_confidence": 4, "ts": "2024-01-01T11:00:00+05:30"},
        "claude": {"recommendation": "enter", "confidence": 4},
        "claude_full": {"recommendation": "enter", "confidence": 4, "chart_analysis": "x"},
    }]})
    client.get("/api/snapshot")                          # builds NIFTY (active); pending builds the rest
    d = client.get("/api/pending").json()
    assert d["index_count"] == 2 and d["stock_count"] == 1 and d["count"] == 3
    idx = {r["symbol"] for r in d["rows"] if r["kind"] == "index"}
    assert idx == {"NIFTY", "BANKNIFTY"}
    stock = next(r for r in d["rows"] if r["kind"] == "stock")
    assert stock["symbol"] == "ACME" and stock["highlight"] and stock["claude_full"]
    assert d["rows"][0]["kind"] == "stock"               # highlights sort first
    # decide the Bank-Nifty row on its OWN instrument → it leaves the inbox, NIFTY stays
    client.post("/api/decision",
                data={"action": "skip", "strategy": "trade1", "ts": t1["ts"], "symbol": "BANKNIFTY"})
    d2 = client.get("/api/pending").json()
    assert d2["index_count"] == 1
    assert {r["symbol"] for r in d2["rows"] if r["kind"] == "index"} == {"NIFTY"}


def test_trigger_read_persists_and_survives_restart(client, monkeypatch):
    """Each auto-read is persisted; after a restart (in-memory cache gone) the table still
    shows Claude's verdict via the stored-read fallback."""
    from journal import store
    t1 = _open_trig(ts="2024-01-01T09:18:00+05:30")
    _seed_heads(monkeypatch, trade1=[t1])
    client.get("/api/snapshot")                       # auto-read → persisted
    saved = store.load_trigger_reads("NIFTY", path=srv.JOURNAL_DB)
    assert any(s["strategy"] == "trade1" and s["ts"] == t1["ts"] for s in saved)
    st = srv._st("NIFTY")                             # simulate a restart
    st["reads"] = {}; st["stored_reads"] = {}; st["stored_reads_at"] = 0.0
    rows = client.get("/api/triggers?strategy=trade1").json()["triggers"]
    row = next(r for r in rows if r["ts"] == t1["ts"])
    assert row["read"] is not None and row["read"]["recommendation"] == "stand_down"


def test_reask_reruns_claude_for_a_trigger(client, monkeypatch):
    """Re-ask re-runs Claude for a trigger on demand (fresh verdict overwrites the cache);
    an unknown ts is a 409."""
    t1 = _open_trig(ts="2024-01-01T09:18:00+05:30")
    _seed_heads(monkeypatch, trade1=[t1])
    client.get("/api/snapshot")
    assert srv._st("NIFTY")["reads"][("trade1", t1["ts"])]["recommendation"] == "stand_down"
    monkeypatch.setattr(srv, "READ_COMPLETER", lambda system, user: ClaudeRead(
        agrees_with_engine=True, chart_analysis="c", oi_analysis="o", where_moving="w",
        right_trade="r", challenge="ch", recommendation="enter", confidence=5, key_risk="k"))
    r = client.post("/api/reask", data={"strategy": "trade1", "ts": t1["ts"]})
    assert r.status_code == 200 and r.json()["recommendation"] == "enter"
    assert srv._st("NIFTY")["reads"][("trade1", t1["ts"])]["recommendation"] == "enter"
    assert client.post("/api/reask", data={"strategy": "trade1", "ts": "nope"}).status_code == 409


def test_breadth_endpoint(client, monkeypatch):
    """/api/breadth computes advance/decline + top contributors off the cached scan rows + the
    live NIFTY spot (no extra pull)."""
    import types
    monkeypatch.setattr(srv, "_SCAN", {"at": 1.0, "rows": [
        {"symbol": "HDFCBANK", "pct_change": 1.0, "open": 1600, "high": 1620, "low": 1595, "close": 1616, "volume": 100},
        {"symbol": "RELIANCE", "pct_change": -2.0, "open": 2900, "high": 2905, "low": 2850, "close": 2842, "volume": 200},
        {"symbol": "INFY", "pct_change": 0.0, "open": 1500, "high": 1505, "low": 1495, "close": 1500, "volume": 50},
    ]})
    srv._st("NIFTY")["snap"] = types.SimpleNamespace(spot=24000.0)
    d = client.get("/api/breadth").json()
    assert d["advance"] == 1 and d["decline"] == 1 and d["unchanged"] == 1
    by = {r["symbol"]: r for r in d["rows"]}
    assert by["HDFCBANK"]["contribution"] > 0 and by["RELIANCE"]["contribution"] < 0
    assert d["rows"][0]["symbol"] == "HDFCBANK"        # positive contribution sorts first
    assert d["net_points"] is not None


def test_breadth_empty_when_no_scan(client, monkeypatch):
    monkeypatch.setattr(srv, "_SCAN", {"at": 0.0, "rows": []})
    d = client.get("/api/breadth").json()
    assert d["rows"] == [] and d["advance"] == 0 and d["decline"] == 0 and d["net_points"] is None


def test_market_read_works_without_a_trigger(client, monkeypatch):
    """The manual market read returns Claude's CURRENT view even with no active trigger
    (unlike /api/analyse, which 409s) — for the selected index."""
    _seed_heads(monkeypatch, trade1=[])               # no triggers → no head
    client.get("/api/snapshot")
    assert client.post("/api/analyse?strategy=trade1").status_code == 409   # gated to a trigger
    r = client.post("/api/market-read")
    assert r.status_code == 200
    d = r.json()
    assert d["recommendation"] in ("enter", "stand_down") and d["chart_analysis"]
    assert d.get("ts")                                 # IST stamp surfaced to the UI
    # Persisted so it can be re-opened later in the day (own table, not the track record).
    from journal import store
    saved = store.load_market_reads("NIFTY", path=srv.JOURNAL_DB)
    assert len(saved) == 1 and saved[0]["read"]["chart_analysis"] == d["chart_analysis"]
    assert store.load_records(srv.JOURNAL_DB) == []    # never pollutes decisions/track record


def test_market_reads_history(client, monkeypatch):
    """The saved market reads are browseable per instrument, newest-first, day-filtered."""
    _seed_heads(monkeypatch, trade1=[])
    client.get("/api/snapshot")
    client.post("/api/market-read")                    # NIFTY read
    client.post("/api/market-read")                    # a second NIFTY read
    h = client.get("/api/market-reads").json()
    assert h["symbol"] == "NIFTY" and len(h["rows"]) == 2 and h["days"]
    # newest-first ordering
    assert h["rows"][0]["ts"] >= h["rows"][1]["ts"]
    day = h["days"][0]
    assert len(client.get(f"/api/market-reads?day={day}").json()["rows"]) == 2
    assert client.get("/api/market-reads?day=1999-01-01").json()["rows"] == []
    # per-instrument isolation
    assert client.get("/api/market-reads?symbol=BANKNIFTY").json()["rows"] == []


def _seed_log(db):
    """Seed the journal: a NIFTY trigger Claude read (acted on) + a BANKNIFTY one
    (un-acted, different day) — the persisted source the log/export reads from."""
    from journal import store
    read = dict(recommendation="enter", confidence=4, oi_bias="bullish",
                chart_analysis="ca", oi_analysis="oa", where_moving="wm",
                right_trade="rt", challenge="ch", key_risk="kr", agrees_with_engine=True)
    store.save_trigger_read("NIFTY", "trade1", "2026-06-25T10:15:00+05:30", read, path=db)
    store.save_trigger_read("BANKNIFTY", "orb", "2026-06-24T09:45:00+05:30",
                            dict(read, recommendation="stand_down", oi_bias="bearish"), path=db)
    store.save_decision({"symbol": "NIFTY", "ts": "2026-06-25T10:15:00+05:30",
                         "decision": "approved", "kind": "live",
                         "proposal": {"trade_type": "trade1", "ts": "2026-06-25T10:15:00+05:30",
                                      "direction": "long", "entry": 24000.0, "stop": 23980.0,
                                      "target": 24060.0, "rr_ratio": 1.5},
                         "trigger_label": "genuine", "reason_why": "clean breakout",
                         "outcome": {"status": "win", "points": 60.0, "rupees": 3900.0},
                         "claude_read": read}, path=db)


def test_triggers_log_aggregates_all_instruments(client):
    """Every persisted trigger + Claude rationale, cross-instrument, newest-first; the acted-on
    one also carries the decision/label/reason/outcome. Day + strategy filters work."""
    _seed_log(srv.JOURNAL_DB)
    d = client.get("/api/triggers-log?symbol=all").json()
    syms = {r["symbol"] for r in d["rows"]}
    assert syms == {"NIFTY", "BANKNIFTY"} and d["count"] == 2
    assert d["rows"][0]["ts"] >= d["rows"][1]["ts"]                 # newest-first
    assert set(d["days"]) == {"2026-06-25", "2026-06-24"}
    nifty = next(r for r in d["rows"] if r["symbol"] == "NIFTY")
    assert nifty["chart_analysis"] == "ca" and nifty["claude_reco"] == "enter"
    assert nifty["decision"] == "approved" and nifty["reason_why"] == "clean breakout"
    assert nifty["direction"] == "long" and nifty["outcome"] == "win" and nifty["points"] == 60.0
    bank = next(r for r in d["rows"] if r["symbol"] == "BANKNIFTY")
    assert bank["decision"] is None and bank["claude_reco"] == "stand_down"   # un-acted: rationale only
    # date filter
    one = client.get("/api/triggers-log?date=2026-06-25").json()
    assert one["count"] == 1 and one["rows"][0]["symbol"] == "NIFTY"
    # strategy filter
    assert client.get("/api/triggers-log?strategy=orb").json()["count"] == 1


def test_triggers_export_csv(client):
    """The same log downloads as a CSV attachment with the documented columns."""
    _seed_log(srv.JOURNAL_DB)
    r = client.get("/api/triggers-export?symbol=all")
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    header = lines[0].split(",")
    for col in ("date", "time", "symbol", "strategy", "claude_reco", "reason_why", "key_risk"):
        assert col in header
    assert len(lines) == 1 + 2                                      # header + 2 triggers
    assert "read" not in header                                    # nested dict dropped from CSV
