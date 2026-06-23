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
                      queues={}, heads={}, actioned={}, reads={}, exits={}, records={})
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
                        lambda s, snap, size: q if s == sid else dict(empty))
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
    cond = client.get("/api/triggers?strategy=condor").json()
    assert "summary" in cond and isinstance(cond["triggers"], list)
    assert client.get("/api/triggers?strategy=bogus").status_code == 404


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


def test_record_endpoint_settles_and_grades(client, tmp_path, monkeypatch):
    import json as _json
    from journal.outcomes import grade_process
    log = tmp_path / "d.jsonl"
    log.write_text(_json.dumps({
        "decision": "approved",
        "proposal": {"recommendation": "enter", "direction": "long", "entry": 24000.0,
                     "stop": 23980.0, "target": 24060.0, "size_lots": 75,
                     "ts": "2024-01-01T09:18:00+05:30"}}) + "\n")
    monkeypatch.setattr(srv, "DEFAULT_LOG", str(log))
    client.get("/api/snapshot")
    d = client.get("/api/record").json()
    assert "cells" in d["summary"] and isinstance(d["recent"], list)
    assert d["recent"][0]["process"] == "good"


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
