# CONTEXT.md — Nifty Trading Agent Build (Phase 1, Track 2)

## Who I am / the goal
I'm a Nifty 50 / Bank Nifty weekly **options trader** (ICICI **Breeze** API for data, Zerodha for execution). I'm building a **self-improving options-trading agent**. Honest self-assessment, confirmed over many logged live sessions: **my edge is reading the market; my weakness is execution discipline.** I log live trading sessions into a structured journal as training data for the future agent. This repo is NOT that journal — it is the **Track 2 build** (see below).

## The agent has two separable machines
- **Machine A — Read Engine.** Synthesizes a directional/structural thesis from a 3-layer stack:
  1. **Chart layer** — EMAs, Bollinger Bands, RSI, MACD, my "3-min strategy" (EMA mean-reversion + Bollinger VRL recovery breakout + SMA pullback continuation, remapped to 3-min bars). **UNIVERSAL — available on every instrument that has OHLCV.** This is the portable core and the ONLY layer Track 2 tests.
  2. **OI/options layer** — PCR, OI walls, max pain, IV skew. Rich on liquid index options (Nifty, Bank Nifty), thin/absent on most stocks and FX. **NOT portable. Excluded from Track 2.**
  3. **Geopolitical/macro layer** — headlines, morning scorecard (Dow, GIFT Nifty, Brent, USD/INR, VIX, Nikkei). Regime-setter, not a per-bar signal. Excluded from Track 2.
- **Machine B — Execution Engine.** Runs the trade by immovable rules (strike, size, stop, booking, stand-down, clock). Mostly already specified in my journal: Scale-In Template, six-line pre-trade check, stop-ratchet, time-container. NOT part of Track 2.

## Road path (5 phases) — I am in Phase 1
1. **Phase 1** — generate Nifty journal data (~15–20 sessions; ~5 done) AND build Track 2 (this repo).
2. **Phase 2** — extract + split Machine A (read) and Machine B (rules) into testable artifacts.
3. **Phase 3** — backtest Machine B cold against Breeze history.
4. **Phase 4** — deploy **propose-only** on Nifty via **Hermes Agent + Telegram approval gate**. MUST stay human-in-the-loop (per-order tap) to remain **non-algo under SEBI's April 2026 retail framework**. API keys: Trade+View enabled, **Withdraw disabled**, Docker sandbox.
5. **Phase 5** — port to other instruments **one at a time**, each independently validated. Depth before breadth.

## This repo = Track 2 (breadth test)
**Goal: test the CHART LAYER ONLY across many instruments to find WHERE my edge generalizes.** Run on **stored historical OHLCV in batch** (avoids API rate limits — no live infra, no VPS, no Hermes needed in Phase 1). Three stages:
- **Stage 1 — Directional-read scoring.** Chart stack outputs a single **long / short / flat** call per bar. Score: did price move the called direction over horizon N bars? Output: an **instrument × directional-expectancy table**. Cheap, wide, run everywhere. Kills non-edges before any level-tuning.
- **Stage 2 — Level scoring (Stage-1 survivors only).** Add actual **entry / stop / target** (full 3-min setup). Score R-multiple, win rate, max adverse excursion. Discovers per-instrument calibration (e.g. Nikkei may need a wider stop than Nifty).
- **Stage 3 — Combined per-instrument ruleset** that survived both → Phase 5 deployment candidate.

**Why directional-read FIRST, then levels:** separates "is the read right?" from "are the levels well-placed?" — a losing trade with a correct read + bad stop demands a different fix than a dead edge. Mixed scoring hides which. Read-scoring is the cheap filter that tells me which instruments even deserve the expensive level-tuning.

## Data plumbing
- **Indian (Nifty, Bank Nifty, Fin Nifty, F&O stocks):** ICICI **Breeze** API (I already have `breeze_pull.py` — reuse it). Note: SMA-200 needs ~400-day rolling window per symbol.
- **Global (Dow, Nikkei, DAX, US equities, USD/INR):** **Twelve Data** (free ~800 calls/day — primary), Alpha Vantage or Polygon as alternates.
- Agent reads **OHLCV** and **computes indicators locally** — same indicator code across all instruments, only the data source differs per market. This is exactly why the chart layer ports and the OI layer doesn't.
- Stage 1 pulls each instrument's history ONCE and scores offline → no rate-limit problem.

## KEY BUILD INSTRUCTION — the directional-output rule
The chart stack's indicators won't always agree (e.g. EMA says up, RSI overbought, Bollinger mid-band). The single long/short/flat call must resolve this. **Do NOT hardcode one method. Implement BOTH as a config flag:**
- **Confluence voting** — N-of-M indicators must agree (e.g. 4-of-6) else flat.
- **Hierarchical** — one primary indicator decides, others filter/veto.
Make it a switch so **Stage 1 scoring can empirically test which performs better, per instrument.** Let the backtest decide; don't pre-commit. (My real method may turn out to be hierarchical-with-confluence-confirmation — keep the design flexible enough to express that.)

## Immediate first task for Claude Code
1. Read this file.
2. Set up the repo skeleton: `data/`, `indicators/`, `scoring/`, `results/`, plus a `README.md`.
3. Draft the **directional-output spec** in `indicators/` as BOTH a markdown spec AND a Python stub, with confluence-vs-hierarchical as the config flag described above.
4. Stub the indicator engine (EMA, Bollinger, RSI, MACD, 3-min strategy logic) as reusable functions that take an OHLCV dataframe and return indicator columns — instrument-agnostic.
5. Keep everything git-tracked.

## Working agreements
- Strategy/judgment questions (confluence-vs-hierarchical results, "is this edge real or curve-fitting", road-path calls) → I'll take to a separate strategy chat, not here.
- This repo = implementation only.
- Reuse my existing Breeze code and scanner logic where it exists; don't rebuild from scratch.
