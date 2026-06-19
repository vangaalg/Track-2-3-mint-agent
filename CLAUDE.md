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
scoring/      stage1.py (directional-read scoring + MTF wiring), validate_export.py
tests/        test_mtf_smoke.py (resample, no-lookahead, MTF methods, e2e score)
results/      score tables / reports + decisions.jsonl (agent decision log)
config.example.yaml   copy to config.yaml (gitignored) and edit

# --- Live agent (Phase 1 slice — the end-to-end build, beyond Track 2) ---
feeds/        snapshot.py (multi-TF ladder + chart read), oi.py + breeze_oi.py
              (live option chain), macro.py + td_macro.py (Twelve Data + India VIX)
analysis/     proposal.py (TradeProposal), trade1.py (directional bucket),
              discipline.py (six-line gate). Machine A read + Machine B levels.
agent/        Claude sparring layer (claude-opus-4-8): read.py (one-shot verdict),
              chat.py (interactive spar_turn), prompt.py, memory.py (learning loop),
              SPARRING_PROMPT.md (the constitution). Needs ANTHROPIC_API_KEY.
execution/    breeze_exec.py — PROPOSE-ONLY Breeze adapter (dry-run default)
dashboard/    app.py — Streamlit one-pane: snapshot + proposal + Approve/Reject
journal/      log.py — append-only decision log (results/decisions.jsonl)
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
- [x] Real indicator stack (the trader's actual chart read): EMA 5/45/100/200,
      SMA 20, Bollinger, RSI, MACD, **Supertrend (7/3)**, **CPR pivots** (classic
      daily CPR broadcast onto every TF, no-lookahead). New voters `ema_stack`,
      `supertrend`, `cpr` in `indicators/directional.py`. Supertrend 7/3 + CPR
      broadcast pinned against the trader's real 3-min chart export (19 Jun 2026)
      and checked by `scoring/validate_export.py`. Tested in
      tests/test_engine_indicators.py + tests/test_directional.py.
- [x] **BreezeLoader ported to real HTTP** (`loaders/breeze.py`): ICICI Breeze
      historicalcharts, checksum auth, creds from env (`BREEZE_API_KEY` /
      `BREEZE_API_SECRET` / `BREEZE_SESSION_TOKEN`). No native 3min → pulls
      1minute + resamples. Legacy `breeze_pull.py`/`pull_fn` kept as a fallback.
      Tested (mocked HTTP) in tests/test_breeze_loader.py.
- [ ] Provide live creds: `BREEZE_*` env (Indian) + `TWELVEDATA_API_KEY` env
      (global). Loaders raise clear errors / instruments are skipped until then.
- Note: **live data runs happen on the user's local machine** (open network).
  This web env is network-locked (egress allowlist blocks api.twelvedata.com), so
  use it for dev/tests; do real pulls locally. See README "Run locally". User
  cloning to `E:\Track 2-3 mint` (Windows).
- [x] **Phase-2 journal extraction (chart layer only):** mapped the live trade
      journal → chart features in `indicators/JOURNAL_EXTRACTION.md` (IN/OUT scope
      table + provisional-thresholds register). New voters `regime_45` (close vs
      45-EMA, the master filter) + `ema5_trigger` (3-min entry); `confirm_2_close`
      gate (2-close + volume, zero-vol fallback); squeeze-gated `bollinger_vrl`;
      `sma_pullback` retargeted to the 45-EMA; `vote_three_min` = the journal trio
      (ema5_trigger + bb_vrl + 45-EMA pullback). OI/PCR/VIX/gap-tree + discipline
      stay OUT (separate repo). Tested in tests/test_directional.py +
      tests/test_engine_indicators.py.
- [ ] Calibrate the provisional numeric thresholds against logged data (the
      register in JOURNAL_EXTRACTION.md lists every one).
- [x] **Full-agent build — Phase 1 thin vertical slice (Nifty + Trade 1):**
      monorepo expansion (feeds/ analysis/ execution/ dashboard/ journal/).
      `feeds.build_snapshot` (multi-TF ladder 1m..month + chart read + injectable
      OI/macro), `analysis.propose_trade1` (entry/stop/target/size/deep-ITM
      vehicle) gated by `analysis.discipline` (six-line check → ENTER/STAND_DOWN),
      `execution.breeze_exec.place` (PROPOSE-ONLY, dry-run unless live+EXECUTION_LIVE=1),
      `dashboard/app.py` (Streamlit approve/reject), `journal.log_decision`.
      Decisions confirmed with user: monorepo, thin-slice-first, Breeze+TwelveData+
      NSE feeds, Breeze execution. Tested (mocked) in tests/test_feeds_snapshot.py,
      test_analysis_trade1.py, test_breeze_exec.py.
- [x] **Claude reasoning + sparring layer (`agent/`):** claude_read (Anthropic
      SDK, claude-opus-4-8, structured output; injectable completer) reads the
      snapshot + deterministic proposal against SPARRING_PROMPT.md (journal
      constitution) and challenges the trade → ENTER/STAND_DOWN; `agent.memory`
      distills results/decisions.jsonl back into the system prompt (the learning
      loop). Wired into the dashboard (sidebar toggle). Tested offline (mocked
      completer) in tests/test_agent_read.py. Needs ANTHROPIC_API_KEY (live).
- [x] **Interactive sparring chat + live OI/macro feeds:** `agent.spar_turn`
      (multi-turn chat, trade context pinned in system, injectable completer) +
      dashboard chat box. Live OI via `feeds.breeze_oi.make_chain_fetcher`
      (Breeze option chain → PCR/walls/max-pain; expiry weekday config) and macro
      via `feeds.td_macro.make_quote_fn` (Twelve Data globals + Breeze India VIX;
      GIFT best-effort). Shared `loaders.breeze.get_breeze_client`. STAND-DOWN
      panel now reads clearly. Mocked tests: test_agent_chat / test_feeds_oi_breeze
      / test_feeds_macro_td.
- [x] **Live dashboard + option-chain viz + decoupled Claude:** `st.fragment`
      auto-refresh (chart ~30s, OI/macro ~5min, time-bucket caches); per-strike
      OI mirrored bar chart + LTP table (Altair); `summarise_chain` walls picked
      within an ATM window (fixes far-strike wall bug); Claude runs only on a
      Trade-1 ENTER trigger (deduped per bar) or the manual "Analyse" button —
      not every tick. Expiry confirmed TUESDAY (weekday=1). `merge_chain` carries
      LTP; `build_snapshot` accepts a pre-fetched `macro`. Tests in
      tests/test_feeds_oi_breeze.py.
- [ ] Phase 2 — broaden data further (all TFs/feeds MODELLED into the signal,
      caching/scheduling); confirm Breeze expiry weekday + GIFT source live.
- [ ] Phase 3 — Trade 2/3 buckets + Stage-2 levels (real calibration).
- [ ] Phase 4 — harden Breeze live order path + journal/grading loop.
- [ ] Stage 2 (levels: entry/stop/target, R-multiple) on Stage-1 survivors.
