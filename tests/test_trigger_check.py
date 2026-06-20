"""Trigger-validation harness: parsing, trigger detection, and the squeeze gate."""

from __future__ import annotations

import numpy as np
import pandas as pd

from scoring.trigger_check import (
    build_feats, find_triggers, _bb_vrl_from_bands,
)
from indicators.directional import (
    resolve_direction, journal_trigger_config, squeeze_trigger_config,
)


# --- a tiny export in the platform's verbose format -------------------------- #
_HDR = ('"Date","Open","High","Low","Close",'
        '"Bollinger Bands Top  (20,2,ma,y)","Bollinger Bands Median  (20,2,ma,y)",'
        '"Bollinger Bands Bottom  (20,2,ma,y)","MA  (5,ema,0,n)","MA  (45,ema,0)"')


def _write_export(tmp_path, rows):
    lines = [_HDR]
    for (hh, mm), (o, h, l, c, bu, bm, bl, e5, e45) in rows:
        ts = f"Fri Jun 19 2026 {hh:02d}:{mm:02d}:00 GMT+0530 (India Standard Time)"
        lines.append(f'"{ts}","{o}","{h}","{l}","{c}","{bu}","{bm}","{bl}","{e5}","{e45}"')
    p = tmp_path / "export.txt"
    p.write_text("\n".join(lines))
    return str(p)


def test_platform_mode_parses_and_builds_signals(tmp_path):
    rows = [((11, 24 + i * 3), (100, 101, 99, 100, 100.5, 100, 99.5, 100, 99))
            for i in range(5)]
    path = _write_export(tmp_path, rows)
    feats = build_feats(path, "platform", "Asia/Kolkata", squeeze_window=3, squeeze_pct=0.25)
    for col in ("bb_upper", "bb_lower", "bb_width", "ema_45", "sig_ema5_trigger", "sig_bb_vrl"):
        assert col in feats.columns
    assert str(feats.index.tz) == "Asia/Kolkata"
    assert len(feats) == 5


def test_platform_breakout_pullback_fires_long(tmp_path):
    # bar0 high crosses the band, above the 45-EMA, close>=5-EMA -> arm long.
    # bar1 is the FIRST close below the 5-EMA -> fire long. Row tuple =
    # (open, high, low, close, bb_top, bb_mid, bb_bot, ema_5, ema_45).
    rows = [
        ((11, 24), (100, 101.2, 100.6, 101.0, 100.5, 100, 98.0, 100.0, 99.0)),   # arm (high 101.2>100.5)
        ((11, 27), (101.5, 103.0, 100.5, 101.5, 100.5, 100, 98.0, 103.0, 99.0)),  # close 101.5<5EMA 103 -> fire
        ((11, 30), (101.4, 101.6, 101.2, 101.4, 100.5, 100, 98.0, 103.0, 99.0)),  # flat, no new breach
    ]
    path = _write_export(tmp_path, rows)
    feats = build_feats(path, "platform", "Asia/Kolkata", squeeze_window=3, squeeze_pct=0.25)
    calls = resolve_direction(feats, journal_trigger_config())
    trigs = find_triggers(calls)
    assert len(trigs) == 1 and trigs[0]["direction"] == "long" and trigs[0]["i"] == 1


# --- trigger logic on a constructed feature frame ---------------------------- #
def _feats(close, bb_lower, bb_upper, ema5, mid=100.0):
    n = len(close)
    idx = pd.date_range("2026-06-19 09:15", periods=n, freq="3min", tz="Asia/Kolkata")
    f = pd.DataFrame({
        "close": np.asarray(close, float), "volume": 0.0,
        "bb_lower": np.asarray(bb_lower, float), "bb_upper": np.asarray(bb_upper, float),
        "bb_mid": mid, "ema_5": np.asarray(ema5, float),
    }, index=idx)
    f["bb_width"] = (f["bb_upper"] - f["bb_lower"]) / f["bb_mid"]
    f["sig_ema5_trigger"] = np.sign(f["close"] - f["ema_5"]).astype("int8")
    return f


def test_clean_squeeze_reversal_fires_one_long(tmp_path):
    # bars 0-3 squeezed (narrow band), bar4 pokes BELOW the lower band, bar5 snaps
    # back inside AND above the EMA-5, holds bars 5-7 -> exactly one long trigger.
    close = [100.0, 100.0, 100.0, 100.0, 99.0,  100.2, 100.3, 100.4]
    lower = [99.5,  99.5,  99.5,  99.5,  99.5,  98.5,  98.5,  98.5]   # narrow thru the breach
    upper = [100.5, 100.5, 100.5, 100.5, 100.5, 101.5, 101.5, 101.5]  # expands on recovery
    ema5 =  [100.0] * 8
    f = _bb_vrl_from_bands(_feats(close, lower, upper, ema5), squeeze_window=3, squeeze_pct=0.5)
    calls = resolve_direction(f, squeeze_trigger_config())
    trigs = find_triggers(calls)
    assert len(trigs) == 1
    assert trigs[0]["direction"] == "long" and trigs[0]["i"] >= 5


def test_no_breach_no_triggers():
    # price oscillates around the EMA-5 but never closes outside the band -> no event.
    close = [100.1, 99.9, 100.2, 99.8, 100.1, 99.9, 100.2, 99.8]
    lower = [98.0] * 8
    upper = [102.0] * 8
    ema5 = [100.0] * 8
    f = _bb_vrl_from_bands(_feats(close, lower, upper, ema5), squeeze_window=3, squeeze_pct=0.5)
    calls = resolve_direction(f, squeeze_trigger_config())
    assert find_triggers(calls) == []
    assert (f["sig_bb_vrl"] == 0).all()


def test_find_triggers_flip_rule():
    idx = pd.date_range("2026-06-19 09:15", periods=6, freq="3min", tz="Asia/Kolkata")
    calls = pd.Series(["flat", "long", "long", "flat", "short", "short"], index=idx)
    trigs = find_triggers(calls)
    assert [t["direction"] for t in trigs] == ["long", "short"]
    assert [t["i"] for t in trigs] == [1, 4]   # only the flips into a direction
