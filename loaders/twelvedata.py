"""Twelve Data loader — global instruments (Dow, Nikkei, DAX, US equities,
USD/INR).

Primary global source (free tier ~800 calls/day, ~8/min). We pull a single
fine **intraday base** (3min) and one **daily** series per instrument, then
resample the intermediate timeframes locally (see ``indicators.timeframes``),
so a full 3m/15m/60m/daily/weekly stack costs only 2 calls per instrument.

API key is read from the ``TWELVEDATA_API_KEY`` environment variable. The HTTP
call is isolated in ``_fetch`` so the rest of the pipeline is testable without
network access.
"""

from __future__ import annotations

import os

import pandas as pd

from loaders.base import OHLCVLoader

_BASE_URL = "https://api.twelvedata.com/time_series"


class TwelveDataLoader(OHLCVLoader):
    source = "twelvedata"

    def __init__(self, api_key: str | None = None, cache_dir="data", outputsize: int = 5000):
        super().__init__(cache_dir=cache_dir)
        self.api_key = api_key or os.environ.get("TWELVEDATA_API_KEY")
        self.outputsize = outputsize

    def _fetch(self, symbol: str, interval: str, start, end) -> pd.DataFrame:
        if not self.api_key:
            raise RuntimeError(
                "TwelveDataLoader needs an API key. Set TWELVEDATA_API_KEY or "
                "pass api_key=..."
            )
        # Imported lazily so the package imports without `requests` installed.
        import requests

        params = {
            "symbol": symbol,
            "interval": interval,          # e.g. "3min", "1day"
            "outputsize": self.outputsize,
            "apikey": self.api_key,
            "format": "JSON",
            "order": "ASC",
            "timezone": "Exchange",        # keep exchange-local tz for sessions
        }
        if start is not None:
            params["start_date"] = str(start)
        if end is not None:
            params["end_date"] = str(end)

        resp = requests.get(_BASE_URL, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") == "error":
            raise RuntimeError(f"Twelve Data error: {payload.get('message')}")

        values = payload.get("values", [])
        if not values:
            raise RuntimeError(f"Twelve Data returned no values for {symbol!r}")

        df = pd.DataFrame(values)
        # Columns arrive as: datetime, open, high, low, close, volume (strings).
        # 'volume' is absent for some FX/index symbols -> fill with 0.
        if "volume" not in df.columns:
            df["volume"] = 0.0
        # Base-class _normalise handles indexing, tz, casing, dtype, sort.
        return df
