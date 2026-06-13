"""Stage-1 sweep-loop tests — config parsing + the end-to-end main() run with a
fake loader (no live creds / network needed).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import yaml

import scoring.stage1 as stage1
from scoring.stage1 import load_config, _directional_config_from, _mtf_config_from


REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Synthetic canonical frames (what a loader returns post-normalisation)
# --------------------------------------------------------------------------- #
def _synth_3m(days: int = 10) -> pd.DataFrame:
    rng = np.random.default_rng(3)
    frames = []
    start = pd.Timestamp("2024-01-01 09:15", tz="Asia/Kolkata")
    for d in range(days):
        idx = pd.date_range(
            start + pd.Timedelta(days=d), periods=125, freq="3min", tz="Asia/Kolkata"
        )
        price = 100 + np.cumsum(rng.standard_normal(len(idx)) * 0.2)
        frames.append(
            pd.DataFrame(
                {"open": price, "high": price + 0.3, "low": price - 0.3,
                 "close": price, "volume": rng.integers(100, 1000, len(idx))},
                index=idx,
            )
        )
    df = pd.concat(frames)
    df.index.name = "datetime"
    return df


def _synth_daily(days: int = 120) -> pd.DataFrame:
    rng = np.random.default_rng(5)
    idx = pd.date_range("2023-09-01", periods=days, freq="1D", tz="Asia/Kolkata")
    price = 100 + np.cumsum(rng.standard_normal(days))
    df = pd.DataFrame(
        {"open": price, "high": price + 1, "low": price - 1, "close": price,
         "volume": rng.integers(1000, 5000, days)},
        index=idx,
    )
    df.index.name = "datetime"
    return df


class _GoodLoader:
    def load(self, symbol, interval, start=None, end=None, use_cache=True):
        return _synth_daily() if "day" in interval else _synth_3m()


class _BadLoader:
    def load(self, *a, **k):
        raise RuntimeError("no creds")  # simulates missing API key / breeze_pull


def _fake_get_loader(source, **kwargs):
    return _BadLoader() if source == "breeze" else _GoodLoader()


def _write_cfg(tmp_path: Path, **overrides) -> Path:
    cfg = {
        "horizon": 8,
        "flat_threshold": 0.0,
        "data": {"intraday_days": 40, "daily_days": 800},
        "sweep": {
            "enabled": True,
            "mtf_methods": ["htf_bias_trigger", "cross_tf_confluence", "per_tf_then_vote"],
            "tf_methods": ["confluence", "hierarchical"],
        },
        "timeframes": {
            "base": "3min",
            "resample_intraday": {"15min": "15min", "60min": "60min"},
            "pull_direct": ["1day"],
            "resample_from_daily": {"1week": "1W"},
        },
        "directional": {
            "method": "confluence",
            "voters": ["ema", "macd", "rsi", "bollinger", "three_min"],
            "voter_kwargs": {"rsi": {"mode": "momentum"}, "bollinger": {"mode": "reversion"}},
            "min_agree": 3, "primary": "ema", "confirm_min": 0, "veto": True,
        },
        "mtf": {
            "mtf_method": "htf_bias_trigger", "trigger_tf": "3min",
            "bias_tfs": ["15min", "60min", "1day", "1week"],
            "bias_quorum": 1, "veto": True,
        },
        "instruments": [
            {"name": "GOOD", "source": "twelvedata", "symbol": "X", "session_anchor": "9h15min"},
            {"name": "BAD", "source": "breeze", "symbol": "Y", "session_anchor": "9h15min"},
        ],
    }
    cfg.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


# --------------------------------------------------------------------------- #
def test_mtf_config_rules_cover_bias_tfs():
    cfg = load_config(str(REPO_ROOT / "config.example.yaml"))
    base = _directional_config_from(cfg["directional"])
    mcfg = _mtf_config_from(cfg, base)
    for tf in mcfg.bias_tfs:
        assert tf in mcfg.rules_by_tf, f"missing align rule for bias TF {tf}"
    mcfg.validate()  # would raise if any bias TF lacked a rule


def test_main_sweep_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(stage1, "get_loader", _fake_get_loader)
    cfg_path = _write_cfg(tmp_path)
    out = tmp_path / "stage1.csv"

    rc = stage1.main(["--config", str(cfg_path), "--out", str(out)])
    assert rc == 0
    assert out.exists()
    assert out.with_suffix(".md").exists()  # ranked markdown deliverable

    table = pd.read_csv(out)
    # BAD instrument is skipped; GOOD swept over 3 mtf x 2 tf = 6 rows.
    assert set(table["instrument"]) == {"GOOD"}
    assert len(table) == 6
    assert {"htf_bias_trigger", "cross_tf_confluence", "per_tf_then_vote"} == set(
        table["method"]
    )
    assert {"confluence", "hierarchical"} == set(table["tf_method"])
    for col in ("instrument", "method", "tf_method", "n_signals", "hit_rate",
                "expectancy", "coverage"):
        assert col in table.columns


def test_main_no_sweep_single_row(tmp_path, monkeypatch):
    monkeypatch.setattr(stage1, "get_loader", _fake_get_loader)
    cfg_path = _write_cfg(tmp_path)
    out = tmp_path / "stage1_default.csv"

    stage1.main(["--config", str(cfg_path), "--out", str(out), "--no-sweep"])
    table = pd.read_csv(out)
    assert len(table) == 1  # GOOD, configured default only
    assert table.iloc[0]["method"] == "htf_bias_trigger"


def test_main_all_skipped_exits(tmp_path, monkeypatch):
    # Every instrument on a failing source -> informative SystemExit, no table.
    monkeypatch.setattr(stage1, "get_loader", lambda source, **k: _BadLoader())
    cfg_path = _write_cfg(tmp_path)
    out = tmp_path / "none.csv"
    import pytest

    with pytest.raises(SystemExit):
        stage1.main(["--config", str(cfg_path), "--out", str(out)])
    assert not out.exists()
