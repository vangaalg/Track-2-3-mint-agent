"""Local OHLCV store: merge/dedup + the backtest offline path."""

from __future__ import annotations

import numpy as np
import pandas as pd

from feeds import ohlcv_store as store


def _frame(start, n, tz="Asia/Kolkata"):
    idx = pd.date_range(start, periods=n, freq="1min", tz=tz)
    p = 100 + np.arange(n) * 0.1
    return pd.DataFrame({"open": p, "high": p + 1, "low": p - 1, "close": p,
                         "volume": 100}, index=idx)


def test_merge_save_dedups_and_extends(tmp_path):
    a = _frame("2024-01-01 09:15", 100)
    store.merge_save("NIFTY", "minute", a, root=tmp_path)
    # overlapping + newer window: newest wins, history extends
    b = _frame("2024-01-01 10:00", 120)
    out = store.merge_save("NIFTY", "minute", b, root=tmp_path)
    assert out.index.is_monotonic_increasing and not out.index.has_duplicates
    cov = store.coverage("NIFTY", "minute", root=tmp_path)
    assert cov[0] == a.index.min() and cov[1] == b.index.max()


def test_merge_save_empty_fresh_is_readthrough(tmp_path):
    a = _frame("2024-01-01 09:15", 10)
    store.merge_save("NIFTY", "minute", a, root=tmp_path)
    out = store.merge_save("NIFTY", "minute", a.iloc[:0], root=tmp_path)
    assert len(out) == 10
    assert store.load_ohlcv("MISSING", "minute", root=tmp_path) is None


def test_backtest_offline_uses_store(tmp_path, monkeypatch):
    import scoring.backtest as bt
    from tests.test_backtest import _synth_1m, _synth_daily
    monkeypatch.setattr(store, "STORE_DIR", tmp_path)
    store.merge_save("NIFTY", "minute", _synth_1m(3), root=tmp_path)
    store.merge_save("NIFTY", "day", _synth_daily(), root=tmp_path)
    base, daily = bt._pull("NIFTY", days=0, loader_name="breeze", offline=True)
    assert base is not None and not base.empty and daily is not None
