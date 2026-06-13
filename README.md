# Track 2 — Chart-Layer Breadth Test

This repo is the **Track 2 build** of a self-improving options-trading agent.
It is **not** the live trading journal. Its single job:

> **Test the CHART LAYER ONLY across many instruments, in batch, on stored
> historical OHLCV, to find WHERE the directional edge generalizes.**

See [`CONTEXT.md`](CONTEXT.md) for the full project framing (the two machines,
the 5-phase road path, why the chart layer ports and the OI layer doesn't).

## Why chart-layer-only

The Read Engine has three layers (chart, OI/options, geopolitical). Only the
**chart layer** is *universal* — it needs nothing but OHLCV, so the same
indicator code runs on Nifty, Nikkei, USD/INR, or any US equity. The OI and
macro layers are not portable and are **excluded** from Track 2.

## The three stages

| Stage | What it scores | Output |
|-------|----------------|--------|
| **1 — Directional read** | One `long / short / flat` call per bar. Did price move the called direction over the next *N* bars? | instrument × directional-expectancy table |
| **2 — Levels** (Stage-1 survivors) | Full 3-min setup: entry / stop / target. R-multiple, win rate, MAE. | per-instrument calibration |
| **3 — Combined ruleset** | What survived both → Phase 5 deployment candidate. | deploy candidate |

**Directional read comes first by design:** it separates *"is the read right?"*
from *"are the levels well-placed?"*. A losing trade with a correct read but a
bad stop needs a different fix than a dead edge — and read-scoring is the cheap,
wide filter that decides which instruments even deserve the expensive
level-tuning. Stage 1 is what this skeleton wires up first.

## Repo layout

```
data/         OHLCV pulls, one file per instrument (gitignored; see data/README.md)
indicators/   Instrument-agnostic indicator engine + directional-output resolver
scoring/      Stage 1 directional-read scoring (Stage 2/3 to follow)
results/      Score tables and reports (gitignored except reports)
```

## The directional-output rule (key design constraint)

The chart stack's indicators won't always agree. Resolving them into a single
`long / short / flat` call is **not hardcoded** — it's a config switch between:

- **Confluence voting** — N-of-M indicators must agree, else flat.
- **Hierarchical** — one primary indicator decides; others filter/veto.

This lets Stage 1 **empirically test which resolver wins, per instrument**,
rather than pre-committing. The design is flexible enough to express
*hierarchical-with-confluence-confirmation*. Full spec:
[`indicators/DIRECTIONAL_SPEC.md`](indicators/DIRECTIONAL_SPEC.md).

## Data plumbing

- **Indian (Nifty, Bank Nifty, Fin Nifty, F&O stocks):** ICICI **Breeze** API
  (reuse existing `breeze_pull.py`). SMA-200 needs a ~400-day rolling window.
- **Global (Dow, Nikkei, DAX, US equities, USD/INR):** **Twelve Data**
  (primary), Alpha Vantage / Polygon as alternates.
- The agent reads **OHLCV** and **computes indicators locally** — same
  indicator code everywhere, only the data source differs per market. Stage 1
  pulls each instrument once and scores offline, so there is no rate-limit
  problem.

## Quick start

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # then edit
# Stage 1 scoring entry point (stub):
python -m scoring.stage1 --help
```

## Status

Phase 1 skeleton: directory layout, indicator engine, directional-output
resolver (both methods), and a Stage-1 scoring stub are in place. Indicator
math (EMA / Bollinger / RSI / MACD) is implemented; the 3-min strategy
components and the scoring loop are structured stubs to be filled as the
journal-derived rules are extracted in Phase 2.
