# CLAUDE.md — session handoff / context-save

Read this first in any new chat so we resume perfectly. Keep it updated as the
project moves.

## Repo
- **Name:** Track-2-3-mint-agent (GitHub: `vangaalg/track-2-3-mint-agent`)
- **What it is:** **Track 2** of a self-improving options-trading agent — the
  **chart-layer breadth test only**. NOT the live trading journal, NOT the
  execution engine. Full framing lives in [`CONTEXT.md`](CONTEXT.md); read it
  before doing anything substantive.

## Branch / git
- **Active dev branch:** `claude/dazzling-lamport-7d0je8` — develop, commit, and
  push here. Do **NOT** push to `main` without explicit permission.
- Push with `git push -u origin claude/dazzling-lamport-7d0je8`.
- **Do NOT open a pull request** unless explicitly asked.

## What this repo does (one line)
Score the chart stack's single `long / short / flat` call across many
instruments on stored historical OHLCV, in batch, to find where the edge
generalizes. Three stages: (1) directional read → (2) levels → (3) combined
ruleset. Stage 1 is the cheap wide filter and is what's wired up first.

## Current layout
```
data/         OHLCV pulls per instrument (gitignored)
loaders/      base.py (OHLCVLoader ABC + canonical contract), twelvedata.py,
              breeze.py (HOOK for user's breeze_pull.py); get_loader() registry
indicators/   engine.py (EMA/BB/RSI/MACD/3-min), timeframes.py (resample +
              no-lookahead align), directional.py (single-TF + MTF resolvers),
              DIRECTIONAL_SPEC.md (the spec)
scoring/      stage1.py (directional-read scoring + MTF wiring, sweep loop TODO)
tests/        test_mtf_smoke.py (resample, no-lookahead, MTF methods, e2e score)
results/      score tables / reports
config.example.yaml   copy to config.yaml (gitignored) and edit
```

## Multi-timeframe (confirmed with user)
- Stack: **3m (trigger) · 15m · 60m · daily · weekly**.
- Sourcing: pull **3m base + daily direct**; resample 15m/60m from base, weekly
  from daily. Rate-limit friendly (2 API calls/instrument).
- Default combine: **htf_bias_trigger** (HTF bias-filter + 3m trigger).
  Switchable: `cross_tf_confluence`, `per_tf_then_vote`. Config block `mtf:`.
- Two correctness invariants enforced in `timeframes.py`: session-anchored
  resample bins + **no-lookahead** alignment (HTF bar visible only after close).
  Both have dedicated tests — do not regress them.

## Key design constraint — never violate
The single `long / short / flat` resolver is **not hardcoded**. It is a config
switch between **confluence voting** (N-of-M agree else flat) and
**hierarchical** (one primary decides, others filter/veto), kept flexible
enough to express *hierarchical-with-confluence-confirmation*. The whole point
is to let Stage 1 backtesting decide which wins **per instrument**. See
`indicators/DIRECTIONAL_SPEC.md`.

## Special instructions / working agreements
- This repo = **implementation only**. Strategy/judgment calls
  (confluence-vs-hierarchical results, "real edge or curve-fitting", road-path)
  go to a **separate strategy chat**, not here.
- **Reuse** existing code where it exists — esp. `breeze_pull.py` for Breeze
  pulls. Don't rebuild from scratch.
- Chart layer ONLY. OI/options and geopolitical/macro layers are **excluded**
  from Track 2.
- Data sources: **Breeze** (Indian), **Twelve Data** (global, primary).
  Indicators computed locally so the same code runs everywhere.
- SMA-200 needs ~400-day rolling window per symbol.

## Status / next steps
- [x] Phase-1 skeleton: dirs, indicator engine (math implemented), single-TF
      directional resolver (both methods), Stage-1 scoring primitives.
- [x] Loader layer (Breeze hook + Twelve Data adapter) → canonical OHLCV frame.
- [x] MTF: session-anchored resample + no-lookahead align + MTF resolver (3
      methods) + score_instrument_mtf. Tested in tests/test_mtf_smoke.py.
- [x] Stage-1 config-driven sweep loop in `scoring.stage1.main`: per instrument
      pull 3m+daily → assemble MTF → score the mtf_method × tf_method grid (6
      cells) → write ranked CSV + markdown to results/. Skips instruments whose
      loader can't pull. Tested in tests/test_stage1_sweep.py.
- [ ] Provide live creds: `breeze_pull.py` on path (Indian) + TWELVEDATA_API_KEY
      env (global). Loaders raise clear errors / instruments are skipped until then.
- Note: **live data runs happen on the user's local machine** (open network).
  This web env is network-locked (egress allowlist blocks api.twelvedata.com), so
  use it for dev/tests; do real pulls locally. See README "Run locally". User
  cloning to `E:\Track 2-3 mint` (Windows).
- [ ] Fill 3-min strategy component thresholds from journal rules (Phase 2).
- [ ] Stage 2 (levels: entry/stop/target, R-multiple) on Stage-1 survivors.
