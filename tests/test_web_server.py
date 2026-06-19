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
def client(monkeypatch):
    monkeypatch.setattr(srv, "PULL_FN", lambda sym: (_synth_1m(), _synth_daily()))
    monkeypatch.setattr(srv, "CHAIN_FN", lambda sym: _chain())
    monkeypatch.setattr(srv, "MACRO_FN", lambda sym: {"usd_inr": {"price": 1, "change_pct": 0.1}})
    monkeypatch.setattr(srv, "READ_COMPLETER", lambda system, user: ClaudeRead(
        agrees_with_engine=True, chart_analysis="ca", oi_analysis="oa",
        where_moving="wm", right_trade="rt", challenge="ch", recommendation="stand_down",
        confidence=4, key_risk="kr"))
    monkeypatch.setattr(srv, "CHAT_COMPLETER", lambda system, history: "sparring reply")
    srv._state.update(snap=None, prop=None, chain=None, snap_at=0.0, oi_at=0.0,
                      read=None, analysed_bar=None, chat=[])
    return TestClient(srv.app)


def test_snapshot_returns_chart_oi_chain_proposal(client):
    d = client.get("/api/snapshot").json()
    assert d["spot"] and d["ts"]
    assert "ema_45" in d["chart"]["numbers"]
    assert d["oi"]["call_wall"]["strike"] == 24000.0     # ATM-window wall
    assert any(r["call_extrinsic"] is not None for r in d["chain"])   # time value present
    assert d["proposal"]["recommendation"] in ("enter", "stand_down")
    assert d["chain"], "per-strike chain rows present"


def test_analyse_returns_four_part_read(client):
    client.get("/api/snapshot")
    rd = client.post("/api/analyse").json()
    assert rd["chart_analysis"] == "ca" and rd["oi_analysis"] == "oa"
    assert rd["where_moving"] == "wm" and rd["right_trade"] == "rt"
    assert rd["recommendation"] == "stand_down"


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
    client.get("/api/snapshot")
    d = client.post("/api/decision", data={"action": "reject"}).json()
    assert d["logged"] == "rejected"
    assert (tmp_path / "d.jsonl").exists()


def test_analyse_without_snapshot_409(client):
    assert client.post("/api/analyse").status_code == 409


def test_triggers_endpoint_shape(client):
    client.get("/api/snapshot")
    d = client.get("/api/triggers").json()
    assert set(d) >= {"session", "triggers", "last", "summary"}
    assert set(d["summary"]) >= {"n", "wins", "losses", "open", "net_points", "net_rupees"}
    assert isinstance(d["triggers"], list)
