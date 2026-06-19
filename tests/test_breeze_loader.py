"""BreezeLoader tests — the breeze-connect SDK path with a mocked SDK (no
network, no live session), the 1min->3min resample, and the missing-creds /
session-failure behaviour.
"""

from __future__ import annotations

import sys
import types

import pandas as pd
import pytest

from loaders.breeze import BreezeLoader


def _install_fake_sdk(monkeypatch, payload, captured, fail_session=False):
    """Inject a fake ``breeze_connect`` module whose client returns ``payload``."""
    mod = types.ModuleType("breeze_connect")

    class BreezeConnect:
        def __init__(self, api_key=None):
            captured["api_key"] = api_key

        def generate_session(self, api_secret=None, session_token=None):
            captured["session"] = (api_secret, session_token)
            if fail_session:
                raise ValueError("bad token")

        def get_historical_data_v2(self, **kw):
            captured["call"] = kw
            return payload

    mod.BreezeConnect = BreezeConnect
    monkeypatch.setitem(sys.modules, "breeze_connect", mod)


def _one_minute_payload(n=6, start="2024-01-01 09:15:00"):
    idx = pd.date_range(start, periods=n, freq="1min")
    rows = []
    for i, ts in enumerate(idx):
        price = 100.0 + i
        rows.append({
            "datetime": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "open": price, "high": price + 0.5, "low": price - 0.5,
            "close": price, "volume": 1000 + i,
        })
    return {"Success": rows, "Status": 200, "Error": None}


def _loader(**kw):
    return BreezeLoader(cache_dir=None, api_key="k", api_secret="s",
                        session_token="t", **kw)


def test_missing_creds_raises():
    loader = BreezeLoader(cache_dir=None, api_key=None, api_secret=None,
                          session_token=None)
    with pytest.raises(RuntimeError, match="creds"):
        loader.load("NIFTY", "1day", use_cache=False)


def test_sdk_pull_parses_canonical_frame(monkeypatch):
    captured = {}
    _install_fake_sdk(monkeypatch, _one_minute_payload(), captured)

    df = _loader().load("NIFTY", "minute", use_cache=False)

    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.tz is not None and df.index.is_monotonic_increasing
    assert len(df) == 6
    # The SDK was session-authenticated and asked for the v2 "1minute" interval.
    assert captured["session"] == ("s", "t")
    assert captured["call"]["interval"] == "1minute"
    assert captured["call"]["stock_code"] == "NIFTY"


def test_3min_request_pulls_1minute_and_resamples(monkeypatch):
    captured = {}
    _install_fake_sdk(monkeypatch, _one_minute_payload(n=6), captured)

    df = _loader().load("NIFTY", "3min", use_cache=False)

    assert captured["call"]["interval"] == "1minute"   # no native 3min
    assert len(df) == 2                                 # 6x1min -> 2x3min
    assert df.iloc[0]["open"] == pytest.approx(100.0)
    assert df.iloc[0]["close"] == pytest.approx(102.0)
    assert df.iloc[0]["high"] == pytest.approx(102.5)
    assert df.iloc[0]["volume"] == pytest.approx(1000 + 1001 + 1002)


def test_breeze_error_payload_raises(monkeypatch):
    _install_fake_sdk(monkeypatch, {"Success": None, "Error": "Invalid User Details"}, {})
    with pytest.raises(RuntimeError, match="Invalid User Details"):
        _loader().load("NIFTY", "1day", use_cache=False)


def test_session_failure_raises(monkeypatch):
    _install_fake_sdk(monkeypatch, _one_minute_payload(), {}, fail_session=True)
    with pytest.raises(RuntimeError, match="Breeze session failed"):
        _loader().load("NIFTY", "1day", use_cache=False)
