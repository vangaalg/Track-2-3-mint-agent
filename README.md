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
loaders/      Per-source loaders (Breeze, Twelve Data) → one canonical OHLCV frame
indicators/   Instrument-agnostic engine, multi-timeframe plumbing, directional resolver
scoring/      Stage 1 directional-read scoring (Stage 2/3 to follow)
results/      Score tables and reports (gitignored except reports)
tests/        Smoke tests (resample correctness, no-lookahead, MTF methods)
```

## Multi-timeframe (MTF)

The 3-min strategy is read inside an MTF stack — **3m (trigger) · 15m · 60m ·
daily · weekly (regime)**. We pull a **3m base** + **daily direct** and resample
the rest locally; higher-TF bars are aligned onto the 3m timeline **without
lookahead** (a bar is only visible once it has closed). The default combination
is **HTF bias-filter + 3m trigger** (higher TFs set/veto direction, 3-min fires
the entry), switchable to cross-TF confluence or per-TF-then-vote. See
[`indicators/DIRECTIONAL_SPEC.md`](indicators/DIRECTIONAL_SPEC.md).

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
cp config.example.yaml config.yaml   # then edit instruments/keys
export TWELVEDATA_API_KEY=...        # global instruments
# put your breeze_pull.py on PYTHONPATH for Indian instruments

# Run Stage 1: per instrument, sweep mtf_method x tf_method and write the
# ranked instrument x directional-expectancy table to results/.
python -m scoring.stage1 --config config.yaml
python -m scoring.stage1 --no-sweep                 # configured default only
python -m scoring.stage1 --mtf-method htf_bias_trigger --method confluence
```

Instruments whose loader can't pull (missing key / `breeze_pull.py`) are skipped
with a warning; the rest still run.

## Run locally (live data) — recommended

Live data pulls need open network. **Run them on your own machine**, where
Twelve Data (global) and your `breeze_pull.py` (Indian) are reachable. (A hosted
sandbox may block outbound network via an egress allowlist; this repo is a
local, offline-batch tool by design — no VPS needed.)

**Windows** (cloning to e.g. `E:\Track 2-3 mint` — quote the spaced path):

```bat
git clone https://github.com/vangaalg/Track-2-3-mint-agent "E:\Track 2-3 mint"
cd /d "E:\Track 2-3 mint"
git checkout claude/dazzling-lamport-7d0je8

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

copy config.example.yaml config.yaml      REM then edit instruments

REM set the Twelve Data key for this shell:
set TWELVEDATA_API_KEY=your_key_here       REM cmd.exe
REM  (PowerShell instead:  $env:TWELVEDATA_API_KEY="your_key_here" )

python -m scoring.stage1 --config config.yaml
REM -> results\stage1_expectancy.csv  +  results\stage1_expectancy.md
```

**macOS / Linux:** same steps with `source .venv/bin/activate`,
`cp config.example.yaml config.yaml`, and `export TWELVEDATA_API_KEY=...`.

Notes:
- **Indian instruments (Breeze):** put your `breeze_pull.py` on `PYTHONPATH`
  (Windows: `set PYTHONPATH=C:\path\to\folder`) or drop it in the repo root.
  Without it those instruments are skipped with a warning — the global
  (Twelve Data) instruments still run. So a Twelve-Data-only first run works with
  just the key; the Breeze rows simply skip until `breeze_pull.py` is present.
- The key lives only in your shell's environment — don't commit it (`config.yaml`
  and `.env` are gitignored).
- Each instrument is pulled once and cached to `data\*.parquet`; reruns are
  offline.

## Run the live agent (Phase 1 slice — propose & approve)

Beyond the batch breadth test, the repo now hosts the **end-to-end agent slice**:
it sources a Nifty snapshot, analyses it, proposes a **Trade 1** (directional)
with entry / stop / target / size / vehicle, and shows it on a dashboard where
**you approve or reject every trade**. Nothing fires without your tap, and an
approved order is **dry-run** unless you flip the live toggle *and* set
`EXECUTION_LIVE=1` (the Breeze key must be Trade+View with **Withdraw disabled** —
the SEBI non-algo, human-in-the-loop constraint).

```
feeds/      one market SNAPSHOT (multi-TF OHLCV + OI + macro)
analysis/   chart read + Trade-1 rulebook + the six-line discipline gate
agent/      Claude (claude-opus-4-8) reads + challenges the trade, learns from the log
execution/  propose-only Breeze order adapter (dry-run by default)
dashboard/  Streamlit one pane: snapshot + proposal + Claude's read + Approve / Reject
journal/    append-only decision log (results/decisions.jsonl)
```

The **`agent/` layer is the sparring partner**: the deterministic engine pins the
numbers (entry/stop/target, the six-line gate), and Claude reads them against the
journal, challenges the bias that loses money, and recommends ENTER / STAND-DOWN —
then the decision log is distilled back into its system prompt as memory (the
learning loop). It needs an Anthropic API key (separate from a Claude.ai chat
plan): `set ANTHROPIC_API_KEY=...` before launching. Toggle it off in the sidebar
to run engine-only.

### Two UIs

**Web cockpit (recommended — no-flicker, single screen):** a FastAPI backend +
a lightweight JS page that polls and updates in place (chart ~15s, OI ~5 min) —
dense one-screen layout with the option-chain OI bars (value labels) + time-value
table, the proposal, Claude's read, a manual *Analyse* button, and the chat.

```bash
pip install -r requirements.txt
uvicorn web.server:app          # then open http://localhost:8000
```

**Streamlit (fallback):**

```bash
streamlit run dashboard/app.py
```

Both reuse the same engine and need the same env (`BREEZE_*`,
`TWELVEDATA_API_KEY`, `ANTHROPIC_API_KEY`).

**OI history (for training mode):** the cockpit logs every option chain to
`data/oi/` automatically. To seed the last week of history, run the one-time
backfill (paced; many Breeze calls):

```bash
python -m feeds.oi_backfill --days 7        # reconstruct ~7 days of chain OI
```

Pick a size, press **Refresh snapshot & propose**, read the agent's recommendation
(ENTER / STAND-DOWN) and reasons, then **Approve** (dry-run unless live) or
**Reject** (a logged no-trade — a good decision). This is the thin first slice;
Trade 2/3, full OI/macro modelling, and the live order path land in later phases.

## Status

Phase 1 in place and tested (`pytest -q`, 11 tests): indicator engine, MTF
plumbing (session-anchored resample + no-lookahead alignment), single-TF and
MTF directional resolvers (all methods), per-source loaders, and the
**config-driven Stage-1 sweep loop** (`scoring.stage1.main`) that scores the
mtf_method × tf_method grid per instrument and writes the ranked expectancy
table (CSV + markdown) to `results/`. Indicator math (EMA / Bollinger / RSI /
MACD) is implemented. Remaining: live creds (Twelve Data key / `breeze_pull.py`),
the 3-min strategy component thresholds (Phase-2 journal extraction), and
Stage 2 (levels) on Stage-1 survivors.
