"""Breeze loader — Indian instruments (Nifty, Bank Nifty, Fin Nifty, F&O stocks).

The user already has a working ``breeze_pull.py`` on their machine; this loader
is a thin adapter, NOT a rebuild. Drop ``breeze_pull.py`` onto the path (or set
``pull_fn``) and this slots it into the canonical-loader interface.

SMA-200 needs ~400 days of warm-up: when pulling the daily series, request a
long enough ``start`` so the leading SMA values aren't NaN.
"""

from __future__ import annotations

from typing import Callable

import pandas as pd

from loaders.base import OHLCVLoader


class BreezeLoader(OHLCVLoader):
    source = "breeze"

    def __init__(self, pull_fn: Callable | None = None, cache_dir="data"):
        super().__init__(cache_dir=cache_dir)
        self._pull_fn = pull_fn

    def _resolve_pull_fn(self) -> Callable:
        if self._pull_fn is not None:
            return self._pull_fn
        # HOOK: reuse the user's existing breeze_pull.py when it's on the path.
        try:
            import breeze_pull  # type: ignore
        except ImportError:
            raise RuntimeError(
                "BreezeLoader needs the user's breeze_pull.py. Put it on the "
                "PYTHONPATH (it exposes a pull function), or pass pull_fn=... "
                "Expected signature: pull_fn(symbol, interval, start, end) -> "
                "DataFrame with datetime + OHLCV columns."
            ) from None
        # Accept a few common entrypoint names without prescribing the file.
        for name in ("pull", "fetch", "get_history", "pull_ohlcv"):
            fn = getattr(breeze_pull, name, None)
            if callable(fn):
                return fn
        raise RuntimeError(
            "breeze_pull.py found but no pull entrypoint (tried: pull, fetch, "
            "get_history, pull_ohlcv). Pass pull_fn=... explicitly."
        )

    def _fetch(self, symbol: str, interval: str, start, end) -> pd.DataFrame:
        pull = self._resolve_pull_fn()
        df = pull(symbol, interval, start, end)
        # Base-class _normalise handles indexing, tz, casing, dtype, sort.
        return df
