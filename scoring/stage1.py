"""Stage 1 — directional-read scoring.

Question Stage 1 answers, cheaply and wide, for every instrument:

    Given the chart stack's single long/short/flat call on each bar, did price
    move the called direction over the next N bars?

Output is an **instrument x directional-expectancy table** — the filter that
decides which instruments even deserve Stage-2 level-tuning. It does NOT model
entries/stops/targets (that is Stage 2); it only grades the *read*.

This module provides the scoring primitives AND the config-driven runner: per
instrument, ``main`` pulls 3m+daily via the loaders, builds the MTF stack, and
scores the ``mtf_method x tf_method`` grid, writing a ranked expectancy table
(CSV + markdown) to ``results/``. Instruments whose loader can't pull (missing
key / breeze_pull.py) are skipped with a warning.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from indicators.engine import compute_indicators
from indicators.directional import (
    DirectionalConfig,
    resolve_direction,
    MTFDirectionalConfig,
    resolve_direction_mtf,
)
from indicators.timeframes import resample_ohlcv, build_mtf_features
from loaders import get_loader

# Per-source (base, daily) interval strings. Override via config `data.intervals`.
SOURCE_INTERVALS = {
    "twelvedata": ("3min", "1day"),
    "breeze": ("3minute", "1day"),
}


# --------------------------------------------------------------------------- #
# Forward outcome
# --------------------------------------------------------------------------- #
def forward_return(df: pd.DataFrame, horizon: int, source: str = "close") -> pd.Series:
    """Return over the next ``horizon`` bars: close[t+N]/close[t] - 1."""
    fwd = df[source].shift(-horizon) / df[source] - 1.0
    return fwd.rename(f"fwd_ret_{horizon}")


def grade_calls(
    calls: pd.Series, fwd_ret: pd.Series, flat_threshold: float = 0.0
) -> pd.DataFrame:
    """Grade each non-flat call against the realised forward return.

    A ``long`` is correct if ``fwd_ret > flat_threshold``; a ``short`` if
    ``fwd_ret < -flat_threshold``. Returns a per-bar frame with the call, the
    forward return, the signed return *in the called direction* (the per-bar
    edge contribution), and a correctness flag. Flat bars are excluded.
    """
    df = pd.DataFrame({"direction": calls, "fwd_ret": fwd_ret}).dropna()
    df = df[df["direction"] != "flat"].copy()
    sign = np.where(df["direction"] == "long", 1.0, -1.0)
    df["signed_ret"] = sign * df["fwd_ret"]
    long_ok = (df["direction"] == "long") & (df["fwd_ret"] > flat_threshold)
    short_ok = (df["direction"] == "short") & (df["fwd_ret"] < -flat_threshold)
    df["correct"] = long_ok | short_ok
    return df


@dataclass
class ExpectancyRow:
    """One row of the instrument x directional-expectancy table."""

    instrument: str
    method: str              # mtf_method (or single-TF method for score_instrument)
    n_signals: int
    n_long: int
    n_short: int
    hit_rate: float          # fraction of non-flat calls that were correct
    avg_signed_ret: float    # mean per-bar edge in the called direction
    expectancy: float        # hit_rate-weighted; == avg_signed_ret here, kept
                             # explicit so Stage 2 can swap in R-multiple terms
    coverage: float          # fraction of bars that produced a non-flat call
    tf_method: str = ""      # per-TF resolver method, when swept alongside mtf

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def score_instrument(
    df: pd.DataFrame,
    instrument: str,
    horizon: int,
    cfg: DirectionalConfig,
    indicator_params: dict | None = None,
    flat_threshold: float = 0.0,
) -> ExpectancyRow:
    """Score one instrument's directional read end-to-end.

    df -> indicators -> directional calls -> grade vs N-bar forward return ->
    one expectancy row.
    """
    feats = compute_indicators(df, indicator_params)
    calls = resolve_direction(feats, cfg)
    fwd = forward_return(feats, horizon)
    graded = grade_calls(calls, fwd, flat_threshold)

    n = len(graded)
    n_bars = int(calls.notna().sum())
    return ExpectancyRow(
        instrument=instrument,
        method=cfg.method,
        n_signals=n,
        n_long=int((graded["direction"] == "long").sum()),
        n_short=int((graded["direction"] == "short").sum()),
        hit_rate=float(graded["correct"].mean()) if n else float("nan"),
        avg_signed_ret=float(graded["signed_ret"].mean()) if n else float("nan"),
        expectancy=float(graded["signed_ret"].mean()) if n else float("nan"),
        coverage=(n / n_bars) if n_bars else float("nan"),
    )


def assemble_mtf_frames(
    base_3m: pd.DataFrame,
    daily: pd.DataFrame,
    intraday_rules: dict[str, str] | None = None,
    weekly_rule: str = "1W",
    anchor: str | None = None,
) -> dict[str, pd.DataFrame]:
    """Build the ``{tf_name: OHLCV}`` stack from a 3m base + a daily series.

    Matches the confirmed sourcing decision: pull 3m + daily, resample the rest
    locally. ``intraday_rules`` maps the intraday TF name to its pandas rule
    (default 15m & 60m); ``anchor`` is the session-open offset (e.g. ``"9h15min"``
    for NSE) passed to ``resample_ohlcv``.
    """
    intraday_rules = intraday_rules or {"15min": "15min", "60min": "60min"}
    frames: dict[str, pd.DataFrame] = {"3min": base_3m}
    for tf, rule in intraday_rules.items():
        frames[tf] = resample_ohlcv(base_3m, rule, anchor)
    frames["1day"] = daily
    frames["1week"] = resample_ohlcv(daily, weekly_rule)
    return frames


def _score_from_features(
    feats_by_tf: dict[str, pd.DataFrame],
    frames_by_tf: dict[str, pd.DataFrame],
    instrument: str,
    horizon: int,
    cfg: MTFDirectionalConfig,
    flat_threshold: float = 0.0,
) -> ExpectancyRow:
    """MTF call -> grade vs N-bar forward return on the trigger TF -> one row.

    Takes pre-computed per-TF features so the sweep can build them ONCE per
    instrument and reuse across resolver variants. ``tf_method`` is recorded
    from ``cfg.base.method`` for traceability in the swept table.
    """
    calls = resolve_direction_mtf(feats_by_tf, cfg)

    trigger = frames_by_tf[cfg.trigger_tf]
    fwd = forward_return(trigger, horizon)
    graded = grade_calls(calls, fwd, flat_threshold)

    n = len(graded)
    n_bars = int(calls.notna().sum())
    return ExpectancyRow(
        instrument=instrument,
        method=cfg.mtf_method,
        n_signals=n,
        n_long=int((graded["direction"] == "long").sum()),
        n_short=int((graded["direction"] == "short").sum()),
        hit_rate=float(graded["correct"].mean()) if n else float("nan"),
        avg_signed_ret=float(graded["signed_ret"].mean()) if n else float("nan"),
        expectancy=float(graded["signed_ret"].mean()) if n else float("nan"),
        coverage=(n / n_bars) if n_bars else float("nan"),
        tf_method=cfg.base.method,
    )


def score_instrument_mtf(
    frames_by_tf: dict[str, pd.DataFrame],
    instrument: str,
    horizon: int,
    cfg: MTFDirectionalConfig,
    indicator_params: dict | None = None,
    flat_threshold: float = 0.0,
) -> ExpectancyRow:
    """Score one instrument's MTF directional read end-to-end.

    frames -> per-TF indicators -> MTF directional call -> grade vs N-bar
    forward return on the TRIGGER (3m) timeframe -> one expectancy row.
    """
    feats_by_tf = build_mtf_features(frames_by_tf, indicator_params)
    return _score_from_features(
        feats_by_tf, frames_by_tf, instrument, horizon, cfg, flat_threshold
    )


def build_expectancy_table(rows: list[ExpectancyRow]) -> pd.DataFrame:
    """Collect scored rows into the instrument x directional-expectancy table."""
    return pd.DataFrame([r.as_dict() for r in rows])


_DAILY_TF_NAMES = {"1day", "1d", "daily"}
_DEFAULT_MTF_METHODS = [
    "htf_bias_trigger",
    "cross_tf_confluence",
    "per_tf_then_vote",
]
_DEFAULT_TF_METHODS = ["confluence", "hierarchical"]
_TABLE_COLUMNS = [
    "instrument", "method", "tf_method", "n_signals", "n_long", "n_short",
    "hit_rate", "avg_signed_ret", "expectancy", "coverage",
]


# --------------------------------------------------------------------------- #
# Config -> dataclasses
# --------------------------------------------------------------------------- #
def load_config(path: str) -> dict:
    """Load the YAML config (see config.example.yaml)."""
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


def _directional_config_from(d: dict) -> DirectionalConfig:
    """Map the `directional` block onto DirectionalConfig (defaults fill gaps)."""
    fields = {f.name for f in dataclasses.fields(DirectionalConfig)}
    return DirectionalConfig(**{k: v for k, v in d.items() if k in fields})


def _rules_by_tf(tf_block: dict) -> dict[str, str]:
    """Resample/align rule per non-trigger TF, from the `timeframes` block.

    Intraday TFs keep their resample rule; pulled daily aligns with `1D`;
    weekly-from-daily keeps its rule. Used by align_to_base for the close shift.
    """
    rules: dict[str, str] = {}
    rules.update(tf_block.get("resample_intraday", {}))
    for tf in tf_block.get("pull_direct", []):
        rules[tf] = "1D" if tf in _DAILY_TF_NAMES else tf
    rules.update(tf_block.get("resample_from_daily", {}))
    return rules


def _mtf_config_from(cfg: dict, base: DirectionalConfig) -> MTFDirectionalConfig:
    """Build an MTFDirectionalConfig from the `mtf` + `timeframes` blocks."""
    m = cfg.get("mtf", {})
    tf = cfg.get("timeframes", {})
    return MTFDirectionalConfig(
        base=base,
        trigger_tf=m.get("trigger_tf", tf.get("base", "3min")),
        bias_tfs=m.get("bias_tfs", ["15min", "60min", "1day", "1week"]),
        rules_by_tf=_rules_by_tf(tf),
        mtf_method=m.get("mtf_method", "htf_bias_trigger"),
        bias_quorum=m.get("bias_quorum", 2),
        veto=m.get("veto", True),
    )


def _timeframe_settings(cfg: dict) -> tuple[dict[str, str], str]:
    """Return (intraday_rules, weekly_rule) for assemble_mtf_frames."""
    tf = cfg.get("timeframes", {})
    intraday_rules = tf.get("resample_intraday", {"15min": "15min", "60min": "60min"})
    weekly_rule = tf.get("resample_from_daily", {}).get("1week", "1W")
    return intraday_rules, weekly_rule


def _pull_frames(inst: dict, cfg: dict):
    """Pull the 3m base + daily series for one instrument (2 API calls)."""
    source = inst["source"]
    symbol = inst["symbol"]
    loader = get_loader(source)

    data_cfg = cfg.get("data", {})
    intervals = data_cfg.get("intervals", {}).get(source) or SOURCE_INTERVALS.get(source)
    if not intervals:
        raise RuntimeError(f"no (base, daily) intervals configured for source {source!r}")
    base_iv, daily_iv = intervals

    today = date.today()
    base3m = loader.load(
        symbol, base_iv,
        start=today - timedelta(days=data_cfg.get("intraday_days", 40)),
    )
    daily = loader.load(
        symbol, daily_iv,
        start=today - timedelta(days=data_cfg.get("daily_days", 800)),
    )
    return base3m, daily


def _sweep_grid(cfg: dict, args) -> list[tuple[str, str]]:
    """The (mtf_method, tf_method) cells to score per instrument."""
    sweep = cfg.get("sweep", {})
    enabled = sweep.get("enabled", True) and not args.no_sweep

    if args.mtf_method:
        mtf_methods = [args.mtf_method]
    elif enabled:
        mtf_methods = sweep.get("mtf_methods", _DEFAULT_MTF_METHODS)
    else:
        mtf_methods = [cfg.get("mtf", {}).get("mtf_method", "htf_bias_trigger")]

    if args.method:
        tf_methods = [args.method]
    elif enabled:
        tf_methods = sweep.get("tf_methods", _DEFAULT_TF_METHODS)
    else:
        tf_methods = [cfg.get("directional", {}).get("method", "confluence")]

    return list(product(mtf_methods, tf_methods))


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _fmt(v) -> str:
    return f"{v:.4f}" if isinstance(v, float) else str(v)


def _df_to_md(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    head = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    body = ["| " + " | ".join(_fmt(r[c]) for c in cols) + " |" for _, r in df.iterrows()]
    return "\n".join([head, sep, *body])


def _write_markdown(table: pd.DataFrame, path: Path) -> None:
    best = (
        table.sort_values("expectancy", ascending=False)
        .groupby("instrument", as_index=False)
        .first()
        .sort_values("expectancy", ascending=False)
    )
    keep = ["instrument", "method", "tf_method", "hit_rate", "expectancy",
            "coverage", "n_signals"]
    out = [
        "# Stage 1 — directional-expectancy table",
        "",
        f"_generated {date.today().isoformat()}_",
        "",
        "## Best config per instrument",
        "",
        _df_to_md(best[keep]),
        "",
        "## Full sweep",
        "",
        _df_to_md(table),
        "",
    ]
    path.write_text("\n".join(out))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Stage 1 — directional-read scoring (Track 2)."
    )
    p.add_argument("--config", default="config.yaml", help="path to config YAML")
    p.add_argument(
        "--horizon", type=int, default=None, help="override forward horizon (bars)"
    )
    p.add_argument(
        "--method",
        choices=["confluence", "hierarchical"],
        default=None,
        help="pin the per-TF resolver method (disables the tf_method sweep)",
    )
    p.add_argument(
        "--mtf-method",
        dest="mtf_method",
        choices=_DEFAULT_MTF_METHODS,
        default=None,
        help="pin the MTF combination method (disables the mtf_method sweep)",
    )
    p.add_argument(
        "--no-sweep",
        action="store_true",
        help="run only the configured default (1 row/instrument)",
    )
    p.add_argument(
        "--out",
        default="results/stage1_expectancy.csv",
        help="where to write the expectancy table",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    cfg = load_config(args.config)

    horizon = args.horizon or cfg.get("horizon", 8)
    flat_threshold = cfg.get("flat_threshold", 0.0)
    indicator_params = cfg.get("indicators")
    base_dir_cfg = cfg.get("directional", {})
    intraday_rules, weekly_rule = _timeframe_settings(cfg)
    grid = _sweep_grid(cfg, args)

    rows: list[ExpectancyRow] = []
    for inst in cfg.get("instruments", []):
        name = inst["name"]
        try:
            base3m, daily = _pull_frames(inst, cfg)
        except Exception as exc:  # missing creds / pull failure -> skip + warn
            print(f"[skip] {name}: {exc}", file=sys.stderr)
            continue

        frames = assemble_mtf_frames(
            base3m, daily, intraday_rules, weekly_rule, inst.get("session_anchor")
        )
        feats = build_mtf_features(frames, indicator_params)

        for mtf_method, tf_method in grid:
            base = _directional_config_from({**base_dir_cfg, "method": tf_method})
            mcfg = _mtf_config_from(cfg, base)
            mcfg.mtf_method = mtf_method
            try:
                rows.append(
                    _score_from_features(
                        feats, frames, name, horizon, mcfg, flat_threshold
                    )
                )
            except Exception as exc:
                print(f"[warn] {name} {mtf_method}/{tf_method}: {exc}", file=sys.stderr)

    if not rows:
        raise SystemExit(
            "No instruments scored. Provide TWELVEDATA_API_KEY (global) and/or "
            "breeze_pull.py on the path (Indian), then retry."
        )

    table = (
        build_expectancy_table(rows)[_TABLE_COLUMNS]
        .sort_values(["instrument", "expectancy"], ascending=[True, False])
        .reset_index(drop=True)
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(out, index=False)
    _write_markdown(table, out.with_suffix(".md"))

    print(f"Wrote {len(table)} rows -> {out}  (+ {out.with_suffix('.md').name})")
    print(table.to_string(index=False))
    return 0


if __name__ == "__main__":
    main()
