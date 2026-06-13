"""Instrument-agnostic chart-layer indicator engine for Track 2.

Everything here takes an OHLCV DataFrame and returns indicator columns or a
directional call. No instrument-specific assumptions, no data-source coupling —
that is the whole reason the chart layer ports across markets.
"""

from indicators.engine import (
    ema,
    sma,
    bollinger_bands,
    rsi,
    macd,
    compute_indicators,
)
from indicators.directional import resolve_direction, DirectionalConfig

__all__ = [
    "ema",
    "sma",
    "bollinger_bands",
    "rsi",
    "macd",
    "compute_indicators",
    "resolve_direction",
    "DirectionalConfig",
]
