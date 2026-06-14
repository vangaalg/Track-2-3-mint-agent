"""BreezeLoader tests — auth/parse path with a mocked HTTP call (no network, no
live creds), the 1min->3min resample for 3-minute requests, and the
missing-creds skip behaviour the Stage-1 sweep relies on.
"""

from __future__ import annotations

import sys
import types

import pandas as pd
import pytest

from loaders.breeze import BreezeLoader


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _install_fake_requests(monkeypatch, payload, captured):
    """Swap in a fake ``requests`` module whose get() returns ``payload``."""
    fake = types.ModuleType("requests")

    def _get(url, data=None, headers=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers
        return _FakeResponse(payload)

    fake.get = _get
    monkeypatch.setitem(sys.modules, "requests", fake)
    return captured


def _one_minute_payload(n=6, start="2024-01-01 09:15:00"):
    idx = pd.date_range(start, periods=n, freq="1min")
    rows = []
    for i, ts in enumerate(idx):
        price = 100.0 + i
        rows.append(
            {
                "datetime": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "open": price, "high": price + 0.5, "low": price - 0.5,
                "close": price, "volume": 1000 + i,
            }
        )
    return {"Success": rows, "Status": 200, "Error": None}


def test_missing_creds_raises():
    loader = BreezeLoader(cache_dir=None, api_key=None, api_secret=None,
                          session_token=None)
    with pytest.raises(RuntimeError, match="creds"):
        loader.load("NIFTY", "1day", use_cache=False)


def test_http_pull_parses_canonical_frame(monkeypatch):
    captured = {}
    _install_fake_requests(monkeypatch, _one_minute_payload(), captured)
    loader = BreezeLoader(cache_dir=None, api_key="k", api_secret="s",
                          session_token="t")

    df = loader.load("NIFTY", "1minute", use_cache=False)

    # Canonical contract: tz-aware sorted index + lowercase OHLCV.
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.tz is not None
    assert df.index.is_monotonic_increasing
    assert len(df) == 6
    # Auth headers were assembled.
    assert captured["headers"]["X-AppKey"] == "k"
    assert captured["headers"]["X-Checksum"].startswith("token ")


def test_3min_request_pulls_1minute_and_resamples(monkeypatch):
    captured = {}
    _install_fake_requests(monkeypatch, _one_minute_payload(n=6), captured)
    loader = BreezeLoader(cache_dir=None, api_key="k", api_secret="s",
                          session_token="t")

    df = loader.load("NIFTY", "3min", use_cache=False)

    # Breeze body asked for 1minute (no native 3min), and we resampled 6x1min
    # into 2x3min bars.
    assert '"interval":"1minute"' in captured["data"]
    assert len(df) == 2
    # First 3-min bar aggregates minutes 0,1,2: open=first, high=max, close=last.
    assert df.iloc[0]["open"] == pytest.approx(100.0)
    assert df.iloc[0]["close"] == pytest.approx(102.0)
    assert df.iloc[0]["high"] == pytest.approx(102.5)
    assert df.iloc[0]["volume"] == pytest.approx(1000 + 1001 + 1002)


def test_breeze_error_payload_raises(monkeypatch):
    _install_fake_requests(
        monkeypatch, {"Success": None, "Status": 500, "Error": "bad token"}, {}
    )
    loader = BreezeLoader(cache_dir=None, api_key="k", api_secret="s",
                          session_token="t")
    with pytest.raises(RuntimeError, match="bad token"):
        loader.load("NIFTY", "1day", use_cache=False)
