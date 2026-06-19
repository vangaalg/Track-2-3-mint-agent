"""OI snapshot store + the historical-backfill assembly (offline)."""

from __future__ import annotations

import pandas as pd

from feeds import oi_store
from feeds.oi_backfill import assemble_day, backfill


def _chain(spot_strike):
    return pd.DataFrame({
        "strike": [23950.0, 24000.0, 24050.0],
        "call_oi": [10.0, 90.0, 20.0], "put_oi": [80.0, 95.0, 15.0],
        "call_ltp": [120.0, 70.0, 40.0], "put_ltp": [40.0, 72.0, 110.0],
    })


def test_store_save_list_load_nearest(tmp_path):
    ts1 = pd.Timestamp("2026-06-23 11:00", tz="Asia/Kolkata")
    ts2 = pd.Timestamp("2026-06-23 11:30", tz="Asia/Kolkata")
    oi_store.save_chain("NIFTY", ts1, 24000.0, _chain(24000), base=tmp_path)
    oi_store.save_chain("NIFTY", ts2, 24010.0, _chain(24000), base=tmp_path)

    snaps = oi_store.list_snapshots("NIFTY", base=tmp_path)
    assert len(snaps) == 2 and snaps[0][0] < snaps[1][0]

    # query at 11:20 -> the 11:00 snapshot (nearest at-or-before)
    got = oi_store.load_nearest("NIFTY", pd.Timestamp("2026-06-23 11:20", tz="Asia/Kolkata"),
                                base=tmp_path)
    assert got is not None and got["spot"].iloc[0] == 24000.0
    # query before the first -> None
    assert oi_store.load_nearest("NIFTY", pd.Timestamp("2026-06-23 10:00", tz="Asia/Kolkata"),
                                 base=tmp_path) is None


def _series(ts_index, oi, ltp):
    return pd.DataFrame({"open_interest": oi, "close": ltp}, index=ts_index)


def test_assemble_day_builds_grid_snapshots():
    idx = pd.date_range("2026-06-23 09:15", periods=120, freq="1min", tz="Asia/Kolkata")
    index_bars = pd.DataFrame({"close": 24000.0 + (pd.RangeIndex(len(idx)) * 0.1)}, index=idx)
    strike_series = {
        (24000.0, "call"): _series(idx, [9_000_000] * len(idx), [70.0] * len(idx)),
        (24000.0, "put"): _series(idx, [9_500_000] * len(idx), [72.0] * len(idx)),
        (24050.0, "call"): _series(idx, [2_000_000] * len(idx), [40.0] * len(idx)),
        (24050.0, "put"): _series(idx, [1_000_000] * len(idx), [110.0] * len(idx)),
    }
    snaps = assemble_day(strike_series, index_bars, grid_minutes=30)
    assert len(snaps) >= 3
    ts, spot, chain = snaps[0]
    assert set(chain.columns) == {"strike", "call_oi", "put_oi", "call_ltp", "put_ltp"}
    assert chain.loc[chain["strike"] == 24000.0, "call_oi"].iloc[0] == 9_000_000
    assert spot >= 24000.0


def test_backfill_writes_snapshots_with_mocked_breeze(tmp_path):
    day_idx = pd.date_range("2026-06-22 09:15", periods=60, freq="1min", tz="Asia/Kolkata")
    index_bars = pd.DataFrame({"close": 24000.0}, index=day_idx)

    class FakeClient:
        def get_historical_data_v2(self, **kw):
            rows = [{"datetime": t.strftime("%Y-%m-%d %H:%M:%S"),
                     "open_interest": 1_000_000, "close": 50.0} for t in day_idx]
            return {"Success": rows, "Error": None}

    saved = backfill("NIFTY", days=1, grid_minutes=30, n_strikes=2,
                     client=FakeClient(), index_for_day=lambda d: index_bars,
                     base=tmp_path)
    assert saved >= 2
    assert oi_store.list_snapshots("NIFTY", base=tmp_path)
