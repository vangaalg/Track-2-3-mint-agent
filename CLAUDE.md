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
dashboard/    app.py — Streamlit one-pane (fallback UI)
web/          server.py (FastAPI JSON API over the engine) + static/ (index.html,
              app.js, style.css) — flicker-free single-screen cockpit, polls ~15s
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
- [x] **Flicker-free web cockpit (`web/`):** FastAPI JSON API over the engine
      (server-side TTL caches: snapshot ~60s, OI/macro ~5min) + a static JS page
      that polls ~15s and updates in place (no fade). Dense one screen: chart
      tiles, option-chain Plotly OI bars (value labels) + time-value table
      (walls/shelves marked, deep-ITM low-extrinsic visible), proposal, Claude's
      4-part read with a manual Analyse button + auto-on-ENTER, chat with
      screenshot upload, approve/reject. Streamlit kept as fallback. Tested
      offline (FastAPI TestClient + mocked seams) in tests/test_web_server.py.
- [x] **Self-improving loop — Phase 1 (close the live outcome loop):**
      `journal.outcomes` grades every logged decision on the journal's 2x2
      (process: good/override/no_trade × outcome: win/loss/open via
      `analysis.triggers.simulate_trade`); `settle_log` resolves approved/rejected
      ENTERs against today's bars and persists. `agent.memory.distill_memory`
      now feeds the 2x2 tallies back + a hard "do NOT reinforce dangerous lucky
      wins (Session-002 trap)" line. Cockpit `/api/record` + Track-record panel
      (2x2 cells). Decided with user: grade by PROCESS+outcome, not P&L. Tested in
      tests/test_outcomes.py + test_web_server.py.

- [x] **Self-improving loop — Phase 2 (OI data flywheel):** `feeds.oi_store`
      (parquet snapshot store: save/list/load_nearest under data/oi/<symbol>/) +
      live logging wired into `web.server` (persists each fresh chain). 7-day
      backfill in `feeds.oi_backfill` — pure `assemble_day` (per-strike OI series →
      chain snapshots on a time grid) + paced Breeze `get_historical_data_v2`
      per-strike pull + CLI (`python -m feeds.oi_backfill --days 7`). Chart is NOT
      stored (re-pullable from Breeze history). Tested in tests/test_oi_store.py;
      live backfill verified by user. data/oi/ gitignored.
- [x] **Customizable chart + full-context decision DB (the "save everything" store):**
      Chart now has **1d/1w** timeframes (frames already existed server-side) and a ⚙
      **indicator panel** — per-line colour + show/hide + width (BB/EMA/Supertrend/
      MACD/RSI + candle colours), applied live via Lightweight `applyOptions`, persisted
      to localStorage. New `journal/store.py` (SQLite at results/journal.db) archives the
      WHOLE decision moment at approve/reject: proposal + decision, Claude's full read,
      the entire chat transcript, multi-TF chart datapoints, the raw per-strike chain,
      and every macro value (VIX/USD-INR/US30/Nasdaq/crude). `journal.outcomes.settle_store`
      grades the store on the same 2×2; `agent.memory.distill_context` feeds the past
      reads-vs-outcomes back into Claude's system prompt (learning now). `web.server`:
      `_chart_bundle` (shared serializer), `save_decision` in /api/decision, store wired
      into _run_read + /api/record; `JOURNAL_DB` seam for tests. Decided with user:
      colours+show/hide only, SQLite, feed-learning-now. results/*.db + *.jsonl gitignored.
      Tested in tests/test_journal_store.py + extended test_web_server.py (103 pass).
      Note: US index *futures* + GIFT Nifty still not in the macro feed (free-tier);
      store captures whatever snap.macro holds, so they're picked up automatically later.

## PENDING ROADMAP (keep visible — confirmed with user)
- [x] **Self-improving loop — Phase 3: TRAINING MODE (`/train` tab).** Replay every
      last-7-days 3-min Trade-1 trigger as-it-was and back-train the agent. Mirrors live
      exactly: **data → Claude's read → trader take/skip+target/stop → reveal+compare**.
      `analysis.triggers.list_triggers` (multi-day flip enumeration) + `simulate_intraday`
      (session-bounded outcome); `feeds.snapshot.build_snapshot_at` rebuilds the as-of world
      with NO future leakage (truncate base + causal partial-today daily bar, tz-matched).
      `web.server`: `_train` cache + `TRAIN_PULL_FN` seam + `/train`, `/api/train/triggers`
      (no outcome), `/api/train/case/{tid}` (as-of chart via `_serialize_chart` + OI via
      `oi_store.load_nearest` + Claude read with the live learning memory; outcome hidden),
      `/api/train/answer` (grade take/skip vs known outcome, persist kind="training").
      `journal.store` gained a **`kind`** column (live/training, migrated in init_db);
      `journal.outcomes.grade_training` (take/skip × win/loss → deserved/accept/missed/
      avoided) + `settle_store` skips training rows; `agent.memory.distill_context` labels
      training replays so they feed Claude's memory. Frontend: shared **`web/static/chart.js`**
      (Lightweight module extracted from app.js — same 1m…1w + ⚙ customization on both pages)
      + `train.html`/`train.js` + nav links. Decisions w/ user: show direction, chart+OI only
      (macro never stored historically → "not recorded"), add Claude's read (3-way compare).
      Tested in tests/test_training.py (14) + suite green (117). Macro as-of past triggers
      stays out (live-only); Trade 2/3 training still pending.
  - **Refinements (training UX + an OI bug):** root-caused the "wrong OI at 13:27" — a
    stale committed fixture `data/oi/NIFTY/20240102T152700.parquet` (the 3L/2.5L mock) was
    served because `.gitignore`'s `data/oi/` had an inline comment (never matched → the dir
    was never ignored) AND `oi_store.load_nearest` had no staleness tolerance. Fixed all
    three: removed the fixture, corrected the gitignore line, added `load_nearest(...,
    max_age_min)` (training uses `OI_MAX_AGE_MIN=180`, same-session only) and the case now
    returns `oi_as_of`/`oi_age_min` (UI shows the snapshot time or an honest "not recorded
    — run the backfill"). Plus: optional **reason** textarea (stored in the record +
    surfaced to Claude's memory via `distill_context`), **editable entry** + outcome graded
    on the trader's entry/target/stop, **live R:R** in the form, fixed **2-lot** sizing
    (`TRAIN_LOTS=2`), and a **running cumulative P&L** scoreboard (`GET /api/train/score`,
    realized = taken trades). Tested in tests/test_training.py (18) + suite green (121).
  - **Claude-vs-you head-to-head + dedup:** the game no longer re-asks an answered trigger —
    `/api/train/triggers` flags `answered` (from store ts) and `train.js nextTrigger` walks
    the UNANSWERED ones chronologically (done → "all answered"). Each trigger is now a scored
    round: `train_answer` grades **Claude's** call too (ENTER=take/STAND_DOWN=skip, engine
    levels) on the same 2×2 and stores `proposal.claude_eval`+`agree`; `GET /api/train/record`
    (`_train_record`) tallies the head-to-head — **rounds won** (correct=deserved/avoided,
    wrong=accept/missed; winner = one correct & other not, else tie), **net P&L** (2 lots,
    realized=taken), and **hit-rate**, per side + agreement rate. Reveal shows agreed/
    disagreed + round winner; header shows `Claude X – Y You` + P&L + hit-rate. Explained the
    3-min trigger to the user (generic 6-voter confluence gated by HTF bias — NOT the journal
    trio/confirmation; that alignment is a deferred STRATEGY decision). Tested in
    tests/test_training.py (21) + suite green (124).
- [ ] **Trade 2 (combined-premium / strangle)** bucket: net premium + breakevens,
      combined SL, intraday-only. Own rulebook + proposal + replay + grading.
- [ ] **Trade 3 (expiry-day OTM momentum, Sensex CE)** bucket: rupee-sized,
      volume/OI-unwind confirmed, flat by close. Own rulebook (highest-discipline).
- [x] **Web cockpit candlestick panel:** `GET /api/chart` serialises the snapshot
      feats (OHLC + BB/EMA5-45-100-200/Supertrend/CPR/MACD/RSI); frontend renders a
      Plotly candlestick + overlays + MACD & RSI subplots + CPR lines, with today's
      triggers marked (▲/▼, win/loss/open coloured). Reuses compute_indicators —
      same columns as the trader's Zerodha export. Tested (/api/chart shape).
- [ ] Calibrate provisional thresholds (JOURNAL_EXTRACTION register) on logged data.
- [ ] Confirm Breeze expiry weekday live (TUESDAY=1) + GIFT/macro source; Twelve
      Data free tier lacks indices/commodities (USD/INR works).
- [ ] Phase 4/5 (CONTEXT) — harden Breeze live order path; port to more instruments.
- [ ] Phase 3 — Trade 2/3 buckets + Stage-2 levels (real calibration).
- [ ] Phase 4 — harden Breeze live order path + journal/grading loop.
- [ ] Stage 2 (levels: entry/stop/target, R-multiple) on Stage-1 survivors.
