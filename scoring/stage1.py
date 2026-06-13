"""Stage 1 — directional-read scoring.

Question Stage 1 answers, cheaply and wide, for every instrument:

    Given the chart stack's single long/short/flat call on each bar, did price
    move the called direction over the next N bars?

Output is an **instrument x directional-expectancy table** — the filter that
decides which instruments even deserve Stage-2 level-tuning. It does NOT model
entries/stops/targets (that is Stage 2); it only grades the *read*.

This module provides the scoring primitives plus a CLI skeleton. The data
loader (Breeze reuse / Twelve Data adapter) and the multi-instrument sweep loop
are the remaining TODOs.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

import numpy as np
import pandas as pd

from indicators.engine import compute_indicators
from indicators.directional import DirectionalConfig, resolve_direction


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
    method: str
    n_signals: int
    n_long: int
    n_short: int
    hit_rate: float          # fraction of non-flat calls that were correct
    avg_signed_ret: float    # mean per-bar edge in the called direction
    expectancy: float        # hit_rate-weighted; == avg_signed_ret here, kept
                             # explicit so Stage 2 can swap in R-multiple terms
    coverage: float          # fraction of bars that produced a non-flat call

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


def build_expectancy_table(rows: list[ExpectancyRow]) -> pd.DataFrame:
    """Collect scored rows into the instrument x directional-expectancy table."""
    return pd.DataFrame([r.as_dict() for r in rows])


# --------------------------------------------------------------------------- #
# CLI skeleton
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
        help="override the directional resolver method",
    )
    p.add_argument(
        "--out",
        default="results/stage1_expectancy.csv",
        help="where to write the expectancy table",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    # TODO Phase 1 wiring:
    #   1. load config (config.yaml): instruments, data sources, horizon,
    #      directional block -> DirectionalConfig, indicator params.
    #   2. for each instrument: load stored OHLCV (breeze_pull.py reuse for
    #      Indian; Twelve Data adapter for global), then score_instrument(...).
    #      Optionally sweep both methods / knob grids per instrument.
    #   3. build_expectancy_table(rows).to_csv(args.out).
    raise SystemExit(
        "scoring.stage1: scoring primitives are ready; the data-loader + sweep "
        "loop are not wired yet. See the TODO in main(). "
        f"(parsed args: {vars(args)})"
    )


if __name__ == "__main__":
    main()
