# data/

Stored historical **OHLCV**, one file per instrument. Stage 1 pulls each
instrument's history **once** and scores offline, so there is no API
rate-limit problem.

Raw pulls (`*.csv` / `*.parquet`) are **gitignored** — they are large and
regenerable from the APIs. Keep this README tracked so the directory persists.

## Expected schema

A `DatetimeIndex` plus columns: `open`, `high`, `low`, `close`, `volume`
(lowercase). That is the only contract the indicator engine relies on, which is
why the same code runs on every market.

## Sources

| market | instruments | source | notes |
|--------|-------------|--------|-------|
| Indian | Nifty, Bank Nifty, Fin Nifty, F&O stocks | ICICI **Breeze** API | reuse `breeze_pull.py`; SMA-200 needs ~400-day window |
| Global | Dow, Nikkei, DAX, US equities, USD/INR | **Twelve Data** (primary) | Alpha Vantage / Polygon as alternates |

## Naming convention

`<NAME>_<interval>.parquet`, e.g. `NIFTY_3min.parquet`, `NIKKEI_3min.parquet`.
