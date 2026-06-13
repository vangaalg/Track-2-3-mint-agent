"""Data loaders for Track 2.

Only the *data source* differs per market — every loader returns the SAME
canonical OHLCV frame, so everything downstream (resample -> indicators ->
votes -> resolve -> score) is identical across instruments. That is exactly why
the chart layer ports and the OI layer doesn't.

Resolve a loader by source name (mirrors the VOTERS registry pattern in
``indicators.directional``)::

    from loaders import get_loader
    loader = get_loader("twelvedata")
    df = loader.load("N225", interval="3min", start=..., end=...)
"""

from __future__ import annotations

from loaders.base import OHLCVLoader, CANONICAL_COLUMNS
from loaders.breeze import BreezeLoader
from loaders.twelvedata import TwelveDataLoader

# source name -> loader class
LOADERS: dict[str, type[OHLCVLoader]] = {
    "breeze": BreezeLoader,
    "twelvedata": TwelveDataLoader,
}


def get_loader(source: str, **kwargs) -> OHLCVLoader:
    """Instantiate the loader registered under ``source``."""
    try:
        cls = LOADERS[source]
    except KeyError:
        raise ValueError(
            f"unknown data source: {source!r}. Known: {list(LOADERS)}"
        ) from None
    return cls(**kwargs)


__all__ = [
    "OHLCVLoader",
    "CANONICAL_COLUMNS",
    "BreezeLoader",
    "TwelveDataLoader",
    "LOADERS",
    "get_loader",
]
