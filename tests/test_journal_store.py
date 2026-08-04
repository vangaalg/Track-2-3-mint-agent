"""Full-context SQLite decision store — round-trip + outcome grading (offline)."""

from __future__ import annotations

import pandas as pd

from journal import store
from journal.outcomes import settle_store


def _payload(decision="approved", direction="long"):
    return {
        "ts": "2024-01-01T09:18:00+05:30", "symbol": "NIFTY",
        "decision": decision, "spot": 24000.0,
        "proposal": {"instrument": "NIFTY", "ts": "2024-01-01T09:18:00+05:30",
                     "direction": direction, "recommendation": "enter",
                     "entry": 24000.0, "stop": 23980.0, "target": 24060.0,
                     "size_lots": 75, "rr_ratio": 3.0, "vehicle": "NIFTY 23700 CE",
                     "mtf_confidence": 3, "final_confidence": 4},
        "claude_read": {"agrees_with_engine": True, "chart_analysis": "ca",
                        "oi_analysis": "oa", "where_moving": "wm", "right_trade": "rt",
                        "challenge": "ch", "recommendation": "enter", "confidence": 4,
                        "key_risk": "loses if it breaks the 45-EMA"},
        "chat": [{"role": "user", "content": "why long?"},
                 {"role": "assistant", "content": "regime is up"}],
        "chart": {"3min": {"bars": [{"t": "2024-01-01T09:18:00+05:30", "o": 1, "h": 2,
                                     "l": 0, "c": 1.5, "ema45": 1.4}], "cpr": {"pivot": 1}}},
        "chain": [{"strike": 24000.0, "call_oi": 9e6, "put_oi": 9.5e6,
                   "call_ltp": 50.0, "put_ltp": 60.0}],
        "macro": {"india_vix": {"price": 13.2, "change_pct": -1.1},
                  "usd_inr": {"price": 83.4, "change_pct": 0.1},
                  "us30_dow": {"price": 39000, "change_pct": 0.3},
                  "nasdaq": {"price": 17000, "change_pct": 0.4}},
        "oi_summary": {"pcr": 1.05, "max_pain": 24000.0, "atm": 24000.0},
        "notes": [], "execution": {"status": "dry_run"},
    }


def test_save_and_load_round_trips(tmp_path):
    db = tmp_path / "journal.db"
    rid = store.save_decision(_payload(), path=db)
    assert rid == 1
    rows = store.load_records(db)
    assert len(rows) == 1
    r = rows[0]
    # scalar columns mirrored out for querying
    assert r["decision"] == "approved" and r["direction"] == "long"
    assert r["entry"] == 24000.0 and r["confidence"] == 4
    assert r["agrees_with_engine"] is True
    # full structures preserved
    assert r["claude_read"]["key_risk"].startswith("loses")
    assert r["chat"][0]["content"] == "why long?"
    assert r["chart"]["3min"]["bars"][0]["ema45"] == 1.4
    assert r["chain"][0]["call_oi"] == 9e6
    assert r["macro"]["india_vix"]["price"] == 13.2
    assert r["macro"]["us30_dow"]["change_pct"] == 0.3


def test_engine_conviction_is_a_queryable_column(tmp_path):
    db = tmp_path / "journal.db"
    store.save_decision(_payload(), path=db)
    r = store.load_records(db)[0]
    # engine conviction (final_confidence) stored separately from Claude's confidence
    assert r["final_confidence"] == 4 and r["confidence"] == 4


def test_final_confidence_falls_back_to_mtf(tmp_path):
    db = tmp_path / "journal.db"
    p = _payload()
    p["proposal"]["final_confidence"] = None        # not boosted yet
    store.save_decision(p, path=db)
    assert store.load_records(db)[0]["final_confidence"] == 3   # falls back to mtf_confidence


def test_final_confidence_column_migrates_onto_old_db(tmp_path):
    import sqlite3
    db = tmp_path / "journal.db"
    store.init_db(db)                                  # full current schema…
    with sqlite3.connect(db) as c:                    # …then simulate a pre-column DB
        c.execute("ALTER TABLE decisions DROP COLUMN final_confidence")
    store.save_decision(_payload(), path=db)          # init_db migrates the column back in
    assert store.load_records(db)[0]["final_confidence"] == 4


def test_load_missing_db_is_empty(tmp_path):
    assert store.load_records(tmp_path / "nope.db") == []


def test_settle_store_grades_2x2(tmp_path):
    db = tmp_path / "journal.db"
    store.save_decision(_payload(), path=db)
    # forward 3-min bars that hit the long target (24060)
    idx = pd.date_range("2024-01-01 09:21", periods=5, freq="3min", tz="Asia/Kolkata")
    bars = pd.DataFrame({"open": 24000.0, "high": [24010, 24030, 24065, 24050, 24040],
                         "low": 23990.0, "close": 24055.0,
                         "volume": 1000}, index=idx)
    settled = settle_store({"3min": bars}, path=db)
    assert settled[0]["process_grade"] == "good"
    assert settled[0]["matrix"] == "deserved"          # good process + win
    # persisted back onto the row
    r = store.load_records(db)[0]
    assert r["outcome_status"] == "win" and r["matrix"] == "deserved"
    assert r["outcome"]["points"] == 60.0


def test_trigger_label_and_reason_round_trip(tmp_path):
    db = tmp_path / "journal.db"
    p = _payload()
    p["trigger_label"] = "genuine"
    p["reason_why"] = "genuine: clean close below the 5-EMA, held the 45-EMA (lesson: take it)"
    rid = store.save_decision(p, path=db)
    r = store.load_records(db)[0]
    assert r["trigger_label"] == "genuine"
    assert "clean close" in r["reason_why"]
    # update_reason patches the reason later (live settle) without touching the label
    store.update_reason(rid, "false: lucky bounce off a graze (lesson: skip)", path=db)
    r = store.load_records(db)[0]
    assert r["reason_why"].startswith("false:") and r["trigger_label"] == "genuine"
    # passing a label overwrites it too
    store.update_reason(rid, "genuine: ...", trigger_label="false", path=db)
    assert store.load_records(db)[0]["trigger_label"] == "false"



def test_actioned_round_trip_and_upsert(tmp_path):
    db = tmp_path / "journal.db"
    store.save_actioned("NIFTY", "trade1", "2026-08-03T09:18:00+05:30", "rejected", path=db)
    store.save_actioned("NIFTY", "cpr_st", "2026-08-03T09:21:00+05:30", "approved", path=db)
    store.save_actioned("BANKNIFTY", "trade1", "2026-08-03T09:18:00+05:30", "skipped", path=db)
    got = store.load_actioned("NIFTY", path=db)
    assert got[("trade1", "2026-08-03T09:18:00+05:30")] == "rejected"
    assert got[("cpr_st", "2026-08-03T09:21:00+05:30")] == "approved"
    assert len(got) == 2                                   # scoped per symbol
    # upsert: a later action on the same trigger wins
    store.save_actioned("NIFTY", "trade1", "2026-08-03T09:18:00+05:30", "approved", path=db)
    assert store.load_actioned("NIFTY", path=db)[("trade1", "2026-08-03T09:18:00+05:30")] == "approved"
    assert store.load_actioned("NIFTY", path=tmp_path / "missing.db") == {}


def test_replay_daily_round_trip_upsert_and_scope(tmp_path):
    db = tmp_path / "journal.db"
    s1 = {"n": 4, "wins": 2, "losses": 1, "open": 1, "net_points": 35.5,
          "net_rupees": 4615.0, "hit_rate": 0.67}
    store.save_replay_daily("NIFTY", "trade1", "2026-08-01", s1, path=db)
    store.save_replay_daily("NIFTY", "orb", "2026-08-01", {"n": 1, "wins": 0, "losses": 1,
                            "net_points": -12.0, "net_rupees": -780.0}, path=db)
    store.save_replay_daily("BANKNIFTY", "trade1", "2026-08-01", s1, path=db)
    rows = store.load_replay_daily("NIFTY", path=db)
    assert len(rows) == 2 and {r["strategy"] for r in rows} == {"trade1", "orb"}
    t1 = next(r for r in rows if r["strategy"] == "trade1")
    assert t1["net_rupees"] == 4615.0 and t1["n"] == 4
    # intraday upsert: latest wins (the session-close number sticks)
    store.save_replay_daily("NIFTY", "trade1", "2026-08-01",
                            {**s1, "n": 6, "net_rupees": 7100.0}, path=db)
    rows2 = store.load_replay_daily("NIFTY", path=db)
    t1b = next(r for r in rows2 if r["strategy"] == "trade1")
    assert t1b["n"] == 6 and t1b["net_rupees"] == 7100.0 and len(rows2) == 2
    # scoped per symbol; missing db -> []
    assert len(store.load_replay_daily("BANKNIFTY", path=db)) == 1
    assert store.load_replay_daily("NIFTY", path=tmp_path / "none.db") == []
