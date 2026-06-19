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
                     "size_lots": 75, "rr_ratio": 3.0, "vehicle": "NIFTY 23700 CE"},
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
