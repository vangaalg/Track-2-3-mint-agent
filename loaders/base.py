"""Loader base class + the canonical OHLCV contract.

Every concrete loader (Breeze, Twelve Data, ...) subclasses ``OHLCVLoader`` and
implements ``_fetch``. The base class wraps it with schema normalisation, a
parquet cache (so each instrument is pulled ONCE — Stage 1 scores offline), and
the canonical-contract guarantee the indicator engine relies on.

Canonical contract — what every loader returns:
  * a **tz-aware** ``DatetimeIndex``, sorted ascending, no duplicates
  * lowercase columns ``open, high, low, close, volume`` (float/int)
"""

from __future__ import annotations

import abc
from pathlib import Path

import pandas as pd

# Reuse the engine's column contract so there is a single source of truth.
from indicators.engine import OHLCV_COLUMNS as CANONICAL_COLUMNS


class OHLCVLoader(abc.ABC):
    """Abstract OHLCV loader.

    Args:
        cache_dir: if set, loaded frames are cached as parquet here keyed by
            ``<symbol>_<interval>.parquet`` and reused on subsequent calls.
    """

    #: human-readable source name; set by subclasses.
    source: str = "base"

    def __init__(self, cache_dir: str | Path | None = "data"):
        self.cache_dir = Path(cache_dir) if cache_dir else None

    # --- public API -------------------------------------------------------- #
    def load(
        self,
        symbol: str,
        interval: str,
        start=None,
        end=None,
        use_cache: bool = True,
    ) -> pd.DataFrame:
        """Return a canonical OHLCV frame for ``symbol`` at ``interval``."""
        cache_path = self._cache_path(symbol, interval)
        if use_cache and cache_path and cache_path.exists():
            return self._normalise(pd.read_parquet(cache_path))

        raw = self._fetch(symbol, interval, start, end)
        df = self._normalise(raw)

        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(cache_path)
        return df

    # --- to implement ------------------------------------------------------ #
    @abc.abstractmethod
    def _fetch(self, symbol: str, interval: str, start, end) -> pd.DataFrame:
        """Source-specific pull. Return a frame with a datetime index/column and
        OHLCV columns (any casing); normalisation is handled by the base class.
        """

    # --- shared helpers ---------------------------------------------------- #
    def _cache_path(self, symbol: str, interval: str) -> Path | None:
        if not self.cache_dir:
            return None
        safe = symbol.replace("/", "-").replace(" ", "_")
        return self.cache_dir / f"{safe}_{interval}.parquet"

    @staticmethod
    def _normalise(df: pd.DataFrame) -> pd.DataFrame:
        """Coerce any source frame into the canonical contract."""
        out = df.copy()
        out.columns = [str(c).strip().lower() for c in out.columns]

        # Establish a DatetimeIndex.
        if not isinstance(out.index, pd.DatetimeIndex):
            for cand in ("datetime", "date", "timestamp", "time"):
                if cand in out.columns:
                    out = out.set_index(cand)
                    break
        out.index = pd.to_datetime(out.index)
        if out.index.tz is None:
            # Treat naive timestamps as UTC; callers can re-localise per market.
            out.index = out.index.tz_localize("UTC")

        missing = [c for c in CANONICAL_COLUMNS if c not in out.columns]
        if missing:
            raise ValueError(
                f"loader produced frame missing {missing}; got {list(out.columns)}"
            )
        out = out[list(CANONICAL_COLUMNS)].astype(float)
        out = out[~out.index.duplicated(keep="last")].sort_index()
        out.index.name = "datetime"
        return out
