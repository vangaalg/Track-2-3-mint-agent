"""Offline test of the Railway-Postgres backend (feeds.db) + store routing.

No real Postgres: an in-memory FakeConn emulates the exact SQL feeds.db issues, so the
append -> load round-trips are exercised through the real store modules (oi_store,
oi_summary_store, macro_store) with db enabled. With db disabled (the rest of the suite)
the stores keep using parquet, so this file is the only place the DB path is covered.
"""

import json

import pandas as pd
import pytest

from feeds import db, oi_store, oi_summary_store, macro_store

_SUM_INS = ["symbol", "ts", "spot", "pcr", "max_pain", "atm", "call_wall_strike",
            "call_wall_oi", "put_shelf_strike", "put_shelf_oi", "res_ext1", "res_ext2",
            "sup_ext1", "sup_ext2"]
_SUM_SEL = _SUM_INS[1:]                                       # drops symbol
_CH_INS = ["symbol", "ts", "strike", "spot", "call_oi", "put_oi", "call_ltp", "put_ltp"]
_CH_SEL = ["ts", "spot", "strike", "call_oi", "put_oi", "call_ltp", "put_ltp"]


class FakeCursor:
    def __init__(self, store):
        self.store, self._rows = store, []

    def execute(self, sql, params=()):
        u = " ".join(sql.upper().split())
        s = self.store
        if "CREATE TABLE" in u or "CREATE INDEX" in u:
            return
        if "INSERT INTO OI_SUMMARY" in u:
            r = dict(zip(_SUM_INS, params)); s["oi_summary"][(r["symbol"], r["ts"])] = r
        elif "FROM OI_SUMMARY" in u:
            recs = sorted((r for (sym, _), r in s["oi_summary"].items() if sym == params[0]),
                          key=lambda r: r["ts"])
            self._rows = [tuple(r[c] for c in _SUM_SEL) for r in recs]
        elif "INSERT INTO OI_CHAIN" in u:
            r = dict(zip(_CH_INS, params)); s["oi_chain"][(r["symbol"], r["ts"], r["strike"])] = r
        elif "MAX(TS)" in u:                                  # nearest: latest ts <= target
            tss = [r["ts"] for (sym, _, _), r in s["oi_chain"].items()
                   if sym == params[0] and r["ts"] <= params[1]]
            self._rows = [(max(tss) if tss else None,)]
        elif "FROM OI_CHAIN" in u:
            if len(params) == 2:                             # nearest: one exact ts
                recs = [r for (sym, t, _), r in s["oi_chain"].items()
                        if sym == params[0] and t == params[1]]
            else:                                            # history: whole symbol
                recs = [r for (sym, _, _), r in s["oi_chain"].items() if sym == params[0]]
            recs.sort(key=lambda r: (r["ts"], r["strike"]))
            self._rows = [tuple(r[c] for c in _CH_SEL) for r in recs]
        elif "INSERT INTO MACRO" in u:
            s["macro"][params[0]] = params[1]
        elif "FROM MACRO" in u:
            self._rows = [(t, d) for t, d in sorted(s["macro"].items())]

    def executemany(self, sql, seq):
        for p in seq:
            self.execute(sql, p)

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class FakeConn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return FakeCursor(self.store)


@pytest.fixture
def pg():
    store = {"oi_summary": {}, "oi_chain": {}, "macro": {}}
    db.set_connect(lambda: FakeConn(store))
    yield store
    db.set_connect(None)                                     # back to parquet for the rest of the suite


def test_enabled_toggles_with_injection(pg):
    assert db.enabled() is True
    db.set_connect(None)
    assert db.enabled() is False                             # no DATABASE_URL + no inject
    db.set_connect(lambda: FakeConn(pg))                     # restore for fixture teardown


def test_oi_summary_round_trip(pg):
    summary = {"pcr": 0.77, "max_pain": 24000, "atm": 23800,
               "call_wall": {"strike": 24000, "oi": 1.3e7},
               "put_shelf": {"strike": 23000, "oi": 8.2e6}}
    levels = {"resistance_ext": [23837, 23872], "support_ext": [23763, 23728]}
    oi_summary_store.append_summary("NIFTY", "2026-06-23T15:27:00+05:30", 23795.25,
                                    summary, levels)
    df = oi_summary_store.load_summary("NIFTY")
    assert df is not None and len(df) == 1
    row = df.iloc[0]
    assert row["pcr"] == 0.77 and row["max_pain"] == 24000
    assert row["call_wall_strike"] == 24000 and row["res_ext1"] == 23837
    assert str(df.index[0]).startswith("2026-06-23T15:27:00")


def test_oi_summary_dedup_on_ts(pg):
    ts = "2026-06-23T15:27:00+05:30"
    for pcr in (0.5, 0.9):
        oi_summary_store.append_summary("NIFTY", ts, 100.0, {"pcr": pcr}, {})
    df = oi_summary_store.load_summary("NIFTY")
    assert len(df) == 1 and df.iloc[0]["pcr"] == 0.9          # newest wins


def test_oi_chain_history_and_nearest(pg):
    early = pd.DataFrame({"strike": [100, 200], "call_oi": [10, 20], "put_oi": [5, 8],
                          "call_ltp": [1.0, 2.0], "put_ltp": [3.0, 4.0]})
    late = pd.DataFrame({"strike": [100, 200], "call_oi": [11, 22], "put_oi": [6, 9],
                         "call_ltp": [1.1, 2.2], "put_ltp": [3.1, 4.1]})
    oi_store.save_chain("NIFTY", "2026-06-23T09:20:00+05:30", 150.0, early)
    oi_store.save_chain("NIFTY", "2026-06-23T15:27:00+05:30", 155.0, late)

    hist = oi_store.load_history("NIFTY")
    assert hist is not None and len(hist) == 4                # 2 cycles x 2 strikes
    assert set(hist.columns) >= {"ts", "spot", "strike", "call_oi", "put_oi"}

    one_day = oi_store.load_history("NIFTY", day="2026-06-23")
    assert len(one_day) == 4
    assert oi_store.load_history("NIFTY", day="2026-06-22") is None   # no rows that day

    near = oi_store.load_nearest("NIFTY", "2026-06-23T13:00:00+05:30")
    assert near is not None
    assert str(near["ts"].iloc[0]).startswith("2026-06-23T09:20")    # the at-or-before snapshot

    stale = oi_store.load_nearest("NIFTY", "2026-06-23T15:00:00+05:30", max_age_min=30)
    assert stale is None                                     # 09:20 snapshot is >30 min old


def test_macro_round_trip(pg):
    macro = {"india_vix": {"price": 13.2, "change_pct": -1.1},
             "usdinr": {"price": 83.4, "change_pct": 0.2}}
    macro_store.append_macro(macro, "2026-06-23T15:27:00+05:30")
    df = macro_store.load_macro()
    assert df is not None and len(df) == 1
    assert df.iloc[0]["india_vix_price"] == 13.2 and df.iloc[0]["usdinr_change"] == 0.2


def test_macro_payload_is_valid_json(pg):
    macro_store.append_macro({"x": {"price": 1.0, "change_pct": 0.0}},
                             "2026-06-23T15:27:00+05:30")
    raw = pg["macro"]["2026-06-23T15:27:00+05:30"]
    assert isinstance(json.loads(raw), dict)                 # stored as a jsonb-castable string
