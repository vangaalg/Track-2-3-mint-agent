"""Training mode — multi-day trigger enumeration, as-of reconstruction, grading,
the kind-tagged store, and the /api/train/* endpoints (all offline/mocked)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import analysis.triggers as tg
import web.server as srv
from agent.read import ClaudeRead
from agent.reason import ReasonWhy
from feeds.snapshot import build_snapshot_at
from journal import store
from journal.outcomes import grade_training, settle_store


# --- synthetic data ---------------------------------------------------------- #
def _synth_1m(days: int = 2) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    frames, start = [], pd.Timestamp("2024-01-01 09:15", tz="Asia/Kolkata")
    for d in range(days):
        idx = pd.date_range(start + pd.Timedelta(days=d), periods=375, freq="1min", tz="Asia/Kolkata")
        p = 24000 + np.cumsum(rng.standard_normal(len(idx)))
        frames.append(pd.DataFrame({"open": p, "high": p + 2, "low": p - 2, "close": p,
                                    "volume": rng.integers(100, 1000, len(idx))}, index=idx))
    df = pd.concat(frames); df.index.name = "datetime"; return df


def _synth_daily() -> pd.DataFrame:
    rng = np.random.default_rng(1)
    idx = pd.date_range("2023-11-01", periods=80, freq="1D", tz="Asia/Kolkata")
    p = 24000 + np.cumsum(rng.standard_normal(80) * 20)
    df = pd.DataFrame({"open": p, "high": p + 30, "low": p - 30, "close": p,
                       "volume": rng.integers(1000, 5000, 80)}, index=idx)
    df.index.name = "datetime"; return df


# --- list_triggers (multi-day) + intraday outcome ---------------------------- #
def test_list_triggers_multi_day(monkeypatch):
    d1 = pd.date_range("2024-01-01 09:15", periods=10, freq="3min", tz="Asia/Kolkata")
    d2 = pd.date_range("2024-01-02 09:15", periods=10, freq="3min", tz="Asia/Kolkata")
    index = d1.append(d2)
    calls = pd.Series(
        ["flat", "flat", "long", "long", "long", "flat", "flat", "flat", "flat", "flat",
         "flat", "short", "short", "short", "flat", "flat", "flat", "flat", "flat", "flat"],
        index=index)
    high = [100.4] * 20; low = [99.6] * 20
    low[0] = 99.0            # day-1 session low -> the long's stop (session-low basis)
    high[10] = 101.0         # day-2 session high -> the short's stop (session-high basis)
    high[5] = 102.0          # day-1 long hits target 101
    low[15] = 98.0           # day-2 short hits target 99
    frame3m = pd.DataFrame({"open": 100.0, "high": high, "low": low, "close": 100.0,
                            "volume": 1}, index=index)
    feats3m = pd.DataFrame({"ema_45": 99.0, "supertrend": 98.0, "cpr_pivot": np.nan,
                            "cpr_tc": 101.0, "cpr_bc": 97.0}, index=index)
    monkeypatch.setattr(tg, "resolve_direction_mtf", lambda feats, cfg: calls)

    out = tg.list_triggers({"3min": feats3m}, {"3min": frame3m})
    assert len(out) == 2
    long_t, short_t = out
    # R:R floor: the 1-pt structural target (rr 1.0) is pushed out to 1.5R = 101.5.
    assert long_t["direction"] == "long" and long_t["outcome"] == "win" and long_t["points"] == 1.5
    assert long_t["eng_stop"] == 99.0 and long_t["eng_target"] == 101.5 and long_t["eng_rr"] == 1.5
    assert long_t["mtf_confidence"] == 0     # no HTF feats supplied → 0
    assert short_t["direction"] == "short" and short_t["outcome"] == "win" and short_t["points"] == 1.5
    assert short_t["tid"] == 1 and short_t["date"] == "2024-01-02"


def test_simulate_intraday_bounds_to_session():
    idx = pd.date_range("2024-01-01 09:15", periods=6, freq="3min", tz="Asia/Kolkata")
    frame = pd.DataFrame({"open": 100.0, "high": [100, 100, 105, 100, 100, 100],
                          "low": 99.0, "close": 100.0}, index=idx)
    outcome, exit_px, points = tg.simulate_intraday(frame, idx[0], "long", 100.0, 98.0, 104.0)
    assert outcome == "win" and exit_px == 104.0 and points == 4.0


# --- as-of reconstruction (no future leakage) -------------------------------- #
def test_build_snapshot_at_truncates():
    base, daily = _synth_1m(2), _synth_daily()
    target = pd.Timestamp("2024-01-02 11:00", tz="Asia/Kolkata")
    snap = build_snapshot_at("NIFTY", base, daily, target, macro={})
    assert pd.Timestamp(snap.ts) <= target                     # latest bar at/just before T
    assert snap.frames["1min"].index[-1] <= target             # no future 1m bar
    # the daily series ends with a partial bar for the target session, not a future day
    assert snap.frames["1day"].index[-1].normalize() == target.normalize()


# --- training 2x2 grade ------------------------------------------------------ #
@pytest.mark.parametrize("action,status,cell", [
    ("take", "win", "deserved"), ("take", "loss", "accept"),
    ("skip", "win", "missed"), ("skip", "loss", "avoided"),
    ("take", "open", "open"), ("skip", None, "open"),
])
def test_grade_training(action, status, cell):
    assert grade_training(action, status) == cell


# --- kind-tagged store + settle skips training ------------------------------- #
def _train_payload():
    return {"kind": "training", "ts": "2024-01-02T09:30:00+05:30", "symbol": "NIFTY",
            "decision": "training_take", "spot": 24000.0,
            "proposal": {"direction": "long", "entry": 24000.0, "stop": 23980.0,
                         "target": 24060.0, "recommendation": "enter"},
            "outcome": {"status": "win", "points": 60.0, "rupees": 4500.0},
            "matrix": "deserved", "process_grade": "training_take"}


def test_store_kind_round_trip_and_settle_skips_training(tmp_path):
    db = tmp_path / "journal.db"
    store.save_decision(_train_payload(), path=db)
    store.save_decision({"kind": "live", "ts": "2024-01-02T09:31:00+05:30", "symbol": "NIFTY",
                         "decision": "rejected",
                         "proposal": {"direction": "long", "recommendation": "stand_down"}},
                        path=db)
    trains = store.load_records(db, kind="training")
    assert len(trains) == 1 and trains[0]["matrix"] == "deserved"
    assert trains[0]["outcome_status"] == "win" and trains[0]["kind"] == "training"
    assert len(store.load_records(db, kind="live")) == 1
    # settling must NOT touch the training row's pre-graded cell
    idx = pd.date_range("2024-01-02 09:33", periods=5, freq="3min", tz="Asia/Kolkata")
    bars = pd.DataFrame({"open": 24000.0, "high": 24010.0, "low": 23990.0, "close": 24000.0},
                        index=idx)
    settle_store({"3min": bars}, path=db)
    assert store.load_records(db, kind="training")[0]["matrix"] == "deserved"


# --- /api/train/* endpoints -------------------------------------------------- #
@pytest.fixture
def tclient(monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "JOURNAL_DB", str(tmp_path / "journal.db"))
    monkeypatch.setattr(srv, "TRAIN_PULL_FN", lambda sym, days: (_synth_1m(2), _synth_daily()))
    monkeypatch.setattr(srv, "READ_COMPLETER", lambda system, user: ClaudeRead(
        agrees_with_engine=True, chart_analysis="ca", oi_analysis="oa", where_moving="wm",
        right_trade="rt", challenge="ch", recommendation="stand_down", confidence=3, key_risk="kr"))
    monkeypatch.setattr(srv, "REASON_COMPLETER", lambda system, user: ReasonWhy(
        why="held the 45-EMA and ran to target", trigger_quality="genuine",
        lesson="clean close below the 5-EMA is the tell"))
    # deterministic trigger list at a real 3-min boundary on day 2
    trig = {"tid": 0, "ts": "2024-01-02T09:30:00+05:30", "date": "2024-01-02",
            "direction": "long", "entry": 24000.0, "eng_stop": 23960.0,
            "eng_target": 24080.0, "eng_rr": 2.0, "outcome": "open", "points": 0.0, "rupees": 0.0}
    monkeypatch.setattr(srv, "list_triggers", lambda feats, frames, cfg=None: [trig])
    srv._train.update(symbol=None, base=None, daily=None, frame3m=None,
                      triggers=None, at=0.0, cases={})
    return TestClient(srv.app)


def test_train_triggers_lists_without_outcome(tclient):
    d = tclient.get("/api/train/triggers?days=8").json()
    assert d["n"] == 1
    t = d["triggers"][0]
    assert t["tid"] == 0 and t["direction"] == "long"
    assert "outcome" not in t and "eng_stop" not in t      # the game hides these


def test_train_answer_captures_label_and_reason(tclient):
    tclient.get("/api/train/triggers")
    tclient.get("/api/train/case/0")
    d = tclient.post("/api/train/answer", data={
        "tid": 0, "action": "take", "entry": 24000.0, "target": 24080.0,
        "stop": 23960.0, "reason": "clean breakout pullback", "label": "genuine"}).json()
    # the post-mortem reason-why comes back for the reveal panel
    assert d["label"] == "genuine"
    assert d["reason_why"]["trigger_quality"] == "genuine"
    assert "45-EMA" in d["reason_why"]["why"]
    # stored on the training row -> feeds the learning memory
    rows = srv.store.load_records(srv.JOURNAL_DB, kind="training")
    assert rows[-1]["trigger_label"] == "genuine"
    assert rows[-1]["reason_why"].startswith("genuine:")
    assert rows[-1]["proposal"]["claude_reason"]["lesson"].startswith("clean close")


def test_train_case_has_read_and_no_outcome(tclient):
    tclient.get("/api/train/triggers")
    d = tclient.get("/api/train/case/0?tf=3min&bars=100").json()
    assert d["direction"] == "long" and d["entry"] == 24000.0
    assert d["bars"] and d["read"]["chart_analysis"] == "ca"
    assert d["macro_available"] is False
    assert "mtf_confidence" in d and "mtf_confidence_breakdown" in d
    assert "outcome" not in d and "engine_outcome" not in d


def test_train_answer_reveals_and_persists(tclient):
    tclient.get("/api/train/triggers")
    tclient.get("/api/train/case/0")                       # must open the case first
    r = tclient.post("/api/train/answer",
                     data={"tid": 0, "action": "take", "target": 24100.0, "stop": 23900.0})
    d = r.json()
    assert d["action"] == "take"
    assert d["your_outcome"]["status"] in ("win", "loss", "open")
    assert "engine_outcome" in d and d["claude"]["recommendation"] == "stand_down"
    rows = store.load_records(srv.JOURNAL_DB, kind="training")
    assert len(rows) == 1 and rows[0]["decision"] == "training_take"
    assert rows[0]["claude_read"]["oi_analysis"] == "oa"


def test_train_answer_rejects_bad_levels(tclient):
    tclient.get("/api/train/triggers")
    tclient.get("/api/train/case/0")
    # long trade with target below entry -> 400
    r = tclient.post("/api/train/answer",
                     data={"tid": 0, "action": "take", "target": 23900.0, "stop": 23800.0})
    assert r.status_code == 400


def test_train_answer_entry_reason_rr_2lots(tclient):
    tclient.get("/api/train/triggers")
    tclient.get("/api/train/case/0")
    r = tclient.post("/api/train/answer", data={
        "tid": 0, "action": "take", "entry": 24000.0, "target": 24080.0,
        "stop": 23960.0, "reason": "clean breakout over CPR"})
    d = r.json()
    assert d["rr"] == 2.0                                    # |80| / |40|
    assert d["your_outcome"]["rupees"] == round(d["your_outcome"]["points"] * 75 * 2, 0)
    assert d["engine_outcome"]["rupees"] == round(d["engine_outcome"]["points"] * 75 * 2, 0)
    rows = store.load_records(srv.JOURNAL_DB, kind="training")
    assert rows[0]["proposal"]["reason"] == "clean breakout over CPR"
    assert rows[0]["proposal"]["size_lots"] == 2 and rows[0]["proposal"]["rr_ratio"] == 2.0
    assert d["score"]["takes"] == 1 and d["score"]["lots"] == 2


def test_train_score_endpoint(tclient):
    tclient.get("/api/train/triggers")
    tclient.get("/api/train/case/0")
    tclient.post("/api/train/answer", data={"tid": 0, "action": "take", "entry": 24000.0,
                                            "target": 24080.0, "stop": 23960.0})
    d = tclient.get("/api/train/score").json()
    assert d["lots"] == 2 and d["takes"] == 1 and d["n"] >= 1
    assert "net_points" in d and "net_rupees" in d


def test_train_case_exposes_oi_age_fields(tclient):
    tclient.get("/api/train/triggers")
    d = tclient.get("/api/train/case/0").json()
    assert "oi_as_of" in d and "oi_age_min" in d            # present even when no snapshot


def test_train_answer_records_claude_eval(tclient):
    tclient.get("/api/train/triggers")
    tclient.get("/api/train/case/0")
    d = tclient.post("/api/train/answer",
                     data={"tid": 0, "action": "skip", "entry": 24000.0}).json()
    assert d["agree"] in (True, False)                       # Claude STAND_DOWN vs your skip
    assert d["round_winner"] in ("you", "claude", "tie")
    assert d["record"]["n"] == 1
    ce = store.load_records(srv.JOURNAL_DB, kind="training")[0]["proposal"]["claude_eval"]
    assert ce["action"] == "skip"                            # READ_COMPLETER → stand_down
    assert ce["cell"] in ("deserved", "accept", "missed", "avoided", "open")


def test_train_record_head_to_head(tclient):
    tclient.get("/api/train/triggers")
    tclient.get("/api/train/case/0")
    tclient.post("/api/train/answer", data={"tid": 0, "action": "skip", "entry": 24000.0})
    d = tclient.get("/api/train/record").json()
    assert set(d) >= {"rounds", "you", "claude", "agree", "disagree", "lots"}
    assert d["lots"] == 2
    r = d["rounds"]
    assert r["you"] + r["claude"] + r["ties"] == 1
    assert d["you"]["answered"] == 1 and d["claude"]["answered"] == 1
    assert d["agree"] + d["disagree"] == 1


def test_train_triggers_answered_flag(tclient):
    tclient.get("/api/train/triggers")
    tclient.get("/api/train/case/0")
    tclient.post("/api/train/answer", data={"tid": 0, "action": "skip", "entry": 24000.0})
    d = tclient.get("/api/train/triggers").json()
    assert d["triggers"][0]["answered"] is True              # no longer re-asked


def test_load_nearest_max_age(tmp_path):
    from feeds import oi_store
    chain = pd.DataFrame({"strike": [24000.0], "call_oi": [1.0], "put_oi": [1.0],
                          "call_ltp": [1.0], "put_ltp": [1.0]})
    snap_ts = pd.Timestamp("2024-01-02 13:15", tz="Asia/Kolkata")
    oi_store.save_chain("NIFTY", snap_ts, 24000.0, chain, base=tmp_path)
    near = pd.Timestamp("2024-01-02 13:27", tz="Asia/Kolkata")     # 12 min later
    far = pd.Timestamp("2026-06-18 13:27", tz="Asia/Kolkata")      # 2+ years later
    assert oi_store.load_nearest("NIFTY", near, base=tmp_path, max_age_min=120) is not None
    assert oi_store.load_nearest("NIFTY", far, base=tmp_path, max_age_min=120) is None
    assert oi_store.load_nearest("NIFTY", far, base=tmp_path) is not None   # default = unbounded
