"""Multi-timeframe (MTF) plumbing — shared, source-agnostic.

The 3-min strategy is read inside an MTF stack: **3m (trigger) · 15m · 60m ·
daily · weekly (regime)**. Per the user's decision we pull one fine intraday
**3m base** + **daily direct**, and derive the rest by resampling locally:

    3m  --resample-->  15m, 60m
    1d  --resample-->  1w

This module does ONLY the timeframe mechanics — resampling and the critical
no-lookahead alignment. It depends on ``engine`` (for per-TF indicators) but NOT
on ``directional`` (decision logic lives there), so there is no import cycle.

Two correctness rules enforced here:
  1. **Session anchoring** — intraday bins align to the market open (e.g. NSE
     09:15), not to midnight, so a "15m bar" matches the chart the trader reads.
  2. **No lookahead** — a higher-TF bar is only visible on the base timeline once
     it has CLOSED. ``align_to_base`` shifts each HTF bar's availability to its
     close time before forward-filling onto the 3m index.
"""

from __future__ import annotations

import pandas as pd
from pandas.tseries.frequencies import to_offset

from indicators.engine import compute_indicators

_OHLC_AGG = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum",
}


def resample_ohlcv(
    df_base: pd.DataFrame, rule: str, anchor: str | None = None
) -> pd.DataFrame:
    """Resample a base OHLCV frame up to a coarser ``rule`` (e.g. ``"15min"``,
    ``"60min"``, ``"1D"``, ``"1W"``).

    Bars are labelled at their **open** (``label="left", closed="left"``) so the
    close time is unambiguously ``open + rule`` — which ``align_to_base`` relies
    on for the no-lookahead shift. ``anchor`` is a pandas offset (e.g.
    ``"9h15min"`` for NSE) that shifts intraday bin edges to the session open.
    Empty bins (overnight gaps, weekends) are dropped.
    """
    kw = dict(label="left", closed="left")
    if anchor:
        kw.update(origin="start_day", offset=anchor)
    out = df_base.resample(rule, **kw).agg(_OHLC_AGG)
    return out.dropna(subset=["open"])


def align_to_base(
    htf: pd.Series | pd.DataFrame, base_index: pd.DatetimeIndex, rule: str
) -> pd.Series | pd.DataFrame:
    """Align a higher-TF series/frame onto the base (3m) index WITHOUT lookahead.

    Each HTF bar (labelled at its open) only becomes available at its close,
    ``open + rule``. We shift the index to that availability time and then, for
    every base bar, take the most recent HTF bar that has already closed
    (``merge_asof`` backward). So at base bar *t* you see the last *completed*
    higher-TF bar — never the one still forming.
    """
    off = to_offset(rule)
    shifted = htf.copy()
    shifted.index = shifted.index + off            # availability = bar close
    if isinstance(shifted, pd.Series):
        shifted = shifted.to_frame()
    shifted = shifted.sort_index()

    base = pd.DataFrame(index=pd.DatetimeIndex(base_index).sort_values())
    aligned = pd.merge_asof(
        base, shifted, left_index=True, right_index=True, direction="backward"
    )
    aligned = aligned.reindex(base_index)
    if isinstance(htf, pd.Series):
        return aligned.iloc[:, 0].rename(htf.name)
    return aligned


def build_mtf_features(
    frames_by_tf: dict[str, pd.DataFrame], indicator_params: dict | None = None
) -> dict[str, pd.DataFrame]:
    """Compute the chart-layer indicator set for EACH timeframe's frame.

    Returns ``{tf_name: feature_frame}`` — identical feature engineering per TF
    via the shared ``engine.compute_indicators``. Vote collection and alignment
    happen in ``directional.resolve_direction_mtf`` (which owns the VOTERS), so
    this stays decision-logic-free.
    """
    return {
        tf: compute_indicators(df, indicator_params) for tf, df in frames_by_tf.items()
    }
