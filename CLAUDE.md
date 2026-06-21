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
- [x] **Backtest realism pass (3 fixes from the first live NIFTY run, confirmed with trader).**
      The raw 148-trigger run was contaminated by level/clustering artifacts; fixed: (1) **R:R floor
      1.5** — `analysis.trade1.trade1_levels` now pushes any structural target closer than 1.5×risk
      out to 1.5R (kills the near-zero-point "wins"; `R_MULTIPLE` is a MINIMUM, not a fallback). Shared
      → live + training + backtest. (2) **One position at a time** + (3) **explicit EOD exit** via a new
      opt-in `list_triggers(..., realistic=True)`: skips a fresh trigger while a prior trade is still
      open (a trend that keeps pulling back counts ONCE, not N×) and labels a mark-to-close exit `"eod"`
      (+ `exit_ts`/`exit` on each trigger) instead of `"open"`. New `analysis.triggers._resolve_intraday`
      (returns exit timestamp + eod). Live settle path keeps `"open"` (= "not resolved yet"); only the
      backtest opts in. `scoring.backtest`: `run_backtest` uses `realistic=True`; `_stats`/`aggregate`
      now bucket win/loss/**eod** (hit-rate = target-vs-stop only; pf incl. eod by sign; expectancy =
      net/trade), report header documents it. Tested: R:R-floor (long/short) + realistic dedup+eod in
      test_triggers; eod aggregate in test_backtest; training multi-day expectations updated for the
      floor. Suite green (169). Still pending: a MIN-STOP-DISTANCE floor (tiny session-low stops still
      yield tiny 1.5R targets) is the next tuning knob.
- [x] **Backtest v2 — target-driven levels + Claude take/skip filter (confirmed w/ trader).**
      (A) `trade1_levels(..., target_driven=True)`: anchor on the structural OBJECTIVE ahead and
      derive the SL so reward:risk == R_MULTIPLE exactly (fixes the SL off the target instead of
      gluing it to the session low → kills the fraction-of-a-point instant stop-outs). Falls back to
      the stop-driven journal model when nothing lies ahead. Threaded through `list_triggers(...,
      target_driven=)`; backtest defaults to it (`--levels target|stop`). (B) `scoring.backtest.
      make_claude_filter` runs `claude_read` per trigger against the AS-OF world (`build_snapshot_at`,
      no leakage) → take/skip; `run_backtest(claude_filter=)` tags each trigger `claude` + adds a
      CLAUDE-FILTERED (ENTER-only) report; CLI `--claude` (needs ANTHROPIC_API_KEY, slow). Tested:
      target-driven long/short + fallback (test_triggers), filter split + completer seam (test_backtest).
      Suite green (175).
- [x] **Claude DECIDES the levels (full control, confirmed w/ trader).** Beyond take/skip, Claude now
      sets its OWN target + stop after a trigger: `ClaudeRead.proposed_target`/`proposed_stop` (schema
      + prompt ask for them on ENTER, null on stand-down). `scoring.backtest.clamp_levels` guardrails
      them (correct side of entry, stop capped to 2% of price, R:R floored to 1.5 by pushing the target
      out — never tightens Claude's stop); `make_claude_filter` returns `{verdict,target,stop}` and
      `run_backtest` SIMULATES each taken trade on CLAUDE's clamped levels (via `_resolve_intraday`) for
      the CLAUDE-FILTERED report, tagging each trigger `claude_target/claude_stop/claude_rr`. Verdict-only
      filters still supported (back-compat). Tested: clamp guardrails + Claude-levels sim (test_backtest).
      Suite green (177). PENDING: surface Claude's levels in the LIVE proposal + training reveal (backtest
      validates them first).
- [x] **Min-stop floor (ATR-based, confirmed w/ trader) + Claude-filter diagnostics.** First live `--claude`
      run stood down on ALL 68 triggers and target-driven levels reintroduced tiny stops (14 <5pt from
      entry → engine net WORSE, −166 vs −99). Fixes: (1) `trade1_levels(min_stop=)` widens a too-tight stop
      and (target-driven) pushes the target out to keep R:R; `list_triggers(atr_mult=, atr_period=)` makes
      the floor **ATR-based** (causal Wilder ATR on 3-min; stop ≥ atr_mult×ATR, scales with vol). CLI
      `--atr-mult` (default 1.0), `--min-stop` (fixed, default 0), `--atr-period`. (2) `make_claude_filter`
      now tracks enter/stand_down/**error** counts on `fn.state` + captures the FIRST error traceback +
      verbose per-verdict print; CLI prints a "X enter / Y stand_down / Z ERRORED" summary so we can tell
      genuine stand-downs from masked failures. LIKELY CAUSE of all-stand_down: the historical as-of world
      has NO OI/macro (Claude's edge), so it's conservative — Claude's value is live-only OR needs a
      chart-only backtest prompt (TBD from the diagnostics re-run). Tested: min-stop (target+stop driven),
      ATR floor widens stops, Claude error tracking. Suite green (182).
- [x] **Backtest preflight + `--min-confidence` HTF-trend filter (measurement tool).** Two adds after the
      first live 30-day NIFTY runs (engine net-negative both floors; 1.0×ATR −308 beats 1.5×ATR −554, so
      wider stops just enlarge losses; longs PF 0.57–0.64 bleed in a down-trend tape). (1) `scoring.backtest.
      _preflight(loader)` resolves + TCP-connects the data host (breeze/twelvedata) once with a 5s timeout
      and exits with a VPN/DNS/firewall checklist instead of hanging for hours on getaddrinfo retries (the
      user hit a 2-hour silent hang on a DNS drop). (2) `run_backtest(min_confidence=N)` keeps only
      HTF-aligned triggers (existing `mtf_confidence` 0–5 = price vs the 45-EMA across 15m/30m/1h/daily/
      weekly ≥ N) and adds a CONFIDENCE-FILTERED report alongside the unfiltered one; CLI `--min-confidence
      N`. This MEASURES the trader's "trade with the higher-timeframe trend" hypothesis on real data — it
      does NOT change the live engine (confidence still only sizes; promoting it to a live gate is a deferred
      STRATEGY call, since the trader earlier confirmed HTF = context, not a veto). Tested: confidence
      subset/off-by-default + report render; preflight fast-fail path. Suite green (185).
- [x] **Backtest `--skip-open-min` opening-whipsaw filter + IST output filenames.** First 30-day
      confidence runs showed (a) HTF-alignment WORKS at the aggregate (≥3/5 took −308→−64 pts, pf
      0.82→0.94; the dropped counter-trend trades lost ~244 pts) but longs stay negative even when
      aligned (pf 0.71, hit 17%), and (b) a cluster of instant stop-outs at the 09:15–09:30 open.
      `list_triggers(skip_open_min=N)` drops triggers whose IST time-of-day is before 09:15+N (NSE
      open), applied BEFORE the realistic one-position dedup so it's a real rule, not a post-hoc cut;
      default 0 = off (live/training unchanged). `run_backtest(skip_open_min=)` + CLI `--skip-open-min N`.
      Also: backtest output files now stamp the full **IST date+time** (`backtest_NIFTY_YYYYMMDD_HHMMSS_IST.
      {csv,md}` + a `_generated … IST_` md header) so successive same-day runs no longer overwrite each
      other (trade ts in the CSV were already IST +05:30). Tested: skip-open drops the earliest trigger +
      kept ones are past the cutoff; IST filename/header. Suite green (187).
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
- [x] **Switched the live + training resolver to the journal 3-min strategy.** Confirmed
      with the trader: the SIGNAL is the **3-min trio alone** (`three_min` = ema5_trigger +
      bb_vrl + 45-EMA pullback) **confirmed by 2 closes + expanding volume**; the higher
      timeframes are **trend context only — NOT a gate/veto** ("signal depends on 3-min, not
      HTF"), and **no 45-EMA veto** on the entry. Implementation in `indicators/directional.py`:
      `DirectionalConfig.confirm_closes`/`confirm_vol_window` (applies `confirm_2_close` inside
      `resolve_direction`), a new **`trigger_only`** mtf_method (`_resolve_trigger_only` = the
      pure 3-min read, no bias matrix), NaN-safe `vote_three_min`, and factories
      `journal_trigger_config()` / `journal_mtf_config()`. Wired as `web.server.RESOLVER_CFG`
      into `build_snapshot`/`build_snapshot_at`/`list_triggers`/`replay_today` (live cockpit +
      training); Stage-1 sweep stays config-driven (resolver remains a config switch).
      config.example.yaml default updated (voters [three_min], min_agree 1, confirm 2/20,
      mtf_method trigger_only). Tested in tests/test_directional.py (confirm gate, NaN-safe
      trio, trigger_only ignores a conflicting HTF) + suite green (127). NOTE: warm-up
      artifacts at window start remain an open tuning question. The "three_min = net-sign
      (any 1 can fire)" over-fire was later FIXED via `vote_bb_reversal` (see below).
- [x] **MTF 45-EMA confidence boost (conviction → size).** Confirmed with the trader: the
      3-min trio still FIRES the trade alone, but conviction rises with each higher TF whose
      **45-EMA sits on the signal's side** (price ABOVE it for a long, BELOW for a short) —
      scored **0–5** across **15m/30m/1h/daily/weekly** (NOT a gate). Added the missing
      **30-min** frame (`feeds/snapshot._RESAMPLE_FROM_1M`/`_RESOLVER_TFS`). New
      `indicators/directional.mtf_ema45_alignment` + `mtf_ema45_confidence` (reuse
      `align_to_base` — no-lookahead; current 3-min price vs each TF's last-completed 45-EMA;
      missing/short frames → 0). Surfaced in `_chart_read` (`mtf_confidence` +
      `mtf_confidence_breakdown`), each trigger dict (`list_triggers`/`replay_today`), the web
      `/api/snapshot` + `/api/train/case`, the cockpit/train UI (shared `chart.js::mtfTicks`
      ✓/✗ per TF), and Claude's prompt. **Size scales** the conviction across the journal's
      65–130 band: `analysis/trade1.size_for_confidence` (0→65 … 5→130) drives `propose_trade1`
      (`TradeProposal.mtf_confidence` new); rupee-risk tracks the scaled size. Training P&L
      stays fixed 2-lot (confidence shown, not sized). Decided w/ user: include daily+weekly
      (0–5) AND scale size. Tested in tests/test_directional.py (counting/partial/missing),
      test_analysis_trade1.py (band scaling), test_feeds_snapshot/test_web_server/test_training
      (surfacing) + suite green (131). NOTE: the 65–130 linear map + equal TF weighting are a
      first cut (easy to retune); confidence reads 0 on flat bars (breakdown still shown).
- [x] **Live strike-selection agent + OI-confluence boost (the 3rd/2nd pillars).** Confirmed
      with the trader: three independent pillars (3-min trigger · OI · Claude's holistic read)
      combine for hit-rate. (1) **Strike agent** (`analysis/strike.select_strike`, LIVE only):
      among ITM strikes within 1000 pts, take the **nearest-to-money one whose time-value
      (extrinsic = LTP − intrinsic) ≤ ~25 pts** (theta proxy; tighter tol → deeper), fallback =
      lowest-extrinsic. Runs in `web.server._refresh` over `feeds.oi.chain_table` (the per-strike
      chain lives only in `_state["chain"]`, not on `Snapshot`); `analysis.trade1.apply_strike`
      rewrites the vehicle (`propose_trade1` stays chain-free). (2) **OI boost** (LIVE only):
      Claude now emits `oi_bias` (bullish/bearish/neutral; `agent/read.py` schema + `ClaudeRead`);
      `apply_oi_boost` adds **+1 conviction when oi_bias agrees with the trigger**, re-nudging size
      across 65–130 (capped 5), recomputing rupee-risk — applied in `_run_read` after Claude
      (ENTER/Analyse). New `TradeProposal` fields (selected_strike/vehicle_ltp/vehicle_extrinsic/
      oi_bias/oi_confidence_boost/final_confidence). Prompt now feeds the full stack (RSI/MACD/
      EMAs) + asks for `oi_bias`. Cockpit `renderProposal` shows the picked strike + time-value +
      OI-boost line. Training untouched (no live chain; fixed 2-lot). Decided w/ user: ≤25-pt
      time-value + auto +1. Tested in tests/test_strike.py (5), extended test_analysis_trade1 /
      test_agent_read / test_web_server + suite green (142). NOTE: no Greeks pulled (extrinsic IS
      the trader's theta criterion); ~25-pt cutoff + +1 increment are first cuts, easy to retune.
- [x] **Fixed the 3-min over-firing (event-gated Bollinger reversal).** Trader flagged FAR too
      many triggers. Root cause: `vote_three_min` aggregated the trio as `np.sign(sum)` = net-sign
      OR, so **EMA-5 state alone** (`sign(close−EMA-5)`, flips on every 3-min cross) fired trades.
      Per the journal + trader (confirmed: event-gated, EMA-5 confirms, Bollinger-reversal-only),
      added **`vote_bb_reversal`** (`indicators/directional.py`): a squeeze-gated `sig_bb_vrl`
      breach→revert whose close agrees with `sig_ema5_trigger` ARMS a direction, HELD while the
      EMA-5 holds that side, cleared on an EMA-5 flip; re-entry needs a fresh event (EMA-5 alone
      never arms; 45-EMA pullback excluded). The latch makes the existing `confirm_2_close`
      (2 closes + volume) + `list_triggers` flip-detection fire exactly ONE trigger per confirmed
      reversal. `journal_trigger_config` now `voters=["bb_reversal"]` (was `three_min`); registered
      in VOTERS; `journal_mtf_config`/`trigger_only` + web wiring unchanged. Docs updated
      (config.example, DIRECTIONAL_SPEC, JOURNAL_EXTRACTION). Over-fire check: a 600-bar chop went
      **119 → 2 triggers**. Tested in tests/test_directional.py (arm/hold/exit, EMA-5-alone never
      fires, event must agree, one-trigger-per-reversal) + suite green (146). `three_min` kept for
      experimentation; squeeze params + EMA-5-exit are the next tuning knobs.
- [x] **Backtest engine (`scoring/backtest.py`).** Wraps the existing `analysis.triggers.list_triggers`
      (enumerate every breakout-pullback trigger across a multi-session frame + session-low-stop outcome)
      into a one-call backtest: pull ~N days of 1-min NIFTY (+ long daily) → `feeds.snapshot.build_snapshot`
      → `list_triggers` with the LIVE `journal_mtf_config` → `aggregate` (overall + per-direction + per-day:
      n, W/L/O, hit-rate, net points/₹, avg win/loss, expectancy, profit factor). CLI
      `python -m scoring.backtest --symbol NIFTY --days 30 [--loader breeze] [--lots 1]` pulls (on the
      user's machine — sandbox is network-locked) and writes a ranked CSV + markdown to results/. Pure
      `aggregate`/`run_backtest`/`report_text` tested offline in tests/test_backtest.py (synthetic frames);
      suite green (166). Live 1-month NIFTY run happens locally with creds.
- [x] **Trigger-validation harness (`scoring/trigger_check.py`).** Calibration loop: trader pastes
      a TradingView 3-min export → the tool runs the exact `journal_trigger_config` + prints each
      trigger time + WHY (`--candidates`/`--events`/`--at`, tunable squeeze/confirm). Reuses
      `scoring.validate_export.load_export`; `platform` mode runs the trigger on the export's OWN
      indicator values (isolates the logic), `--recompute` does the whole pipeline. data/validate/
      gitignored. Tested in tests/test_trigger_check.py.
- [x] **Cockpit chart: fixed CPR display + added click-to-draw trend lines.** (1) CPR wasn't showing —
      root cause: `_serialize_chart` recomputed CPR per intraday frame and took the last bar, so a live
      single-session 3-min frame gave NaN (no prior session) → frontend skipped the lines (and the
      `#9aa0b4` dashed colour was faint). Fix: new `web.server._daily_cpr(snap)` sources CPR from the
      DAILY frame (always has a prior session → never NaN), wired into `/api/chart` + `_chart_bundle`;
      `chart.js` now has CPR pivot/TC/BC in the ⚙ panel (show/hide + colour, clearer `#5b6b8c` default)
      via `redrawCpr()` (price-lines redrawn on gear change). (2) Trend lines — `chart.js` drawing
      toolbar (Horizontal / Trend / Clear): `main.subscribeClick` + `candle.coordinateToPrice` →
      horizontal = `createPriceLine`, angled = a 2-point line series; persisted per-TF to localStorage
      (`chartDrawings`), re-applied on refresh + TF switch (`redrawDrawings`), Clear wipes the TF. Shared
      `chart.js` so cockpit + `/train` both get it; toolbar markup in index.html/train.html + style.css.
      Tested: tests/test_web_server.py asserts numeric CPR incl. a single-session intraday case; suite
      green (157). Frontend drawing is manual-verify (no JS test harness). Confirmed w/ user: draw-on-chart.
- [x] **Pinned the 3-min entry against TWO charts → breakout + first-5-EMA-pullback; stop = day low.**
      `bb_reversal` was a backwards fade; a "low touches 5-EMA" cut and then a "VRL retest" cut each
      matched Nifty but the trader's Bank Nifty chart disproved them. Settled mechanic (Nifty +
      Bank Nifty both reproduced): a **breakout** = the bar whose **HIGH crosses the upper band**
      (`high > bb_upper`, close may be inside — Bank Nifty 13:42 high 57608.75) while above the
      45-EMA and at/above the 5-EMA; the **entry = the FIRST bar that CLOSES below the 5-EMA** after
      it (the pullback) — Nifty **14:18** (23965.45<5-EMA 23975.58), Bank Nifty **14:39** (57715.45<
      57725.28). **One entry per setup; a fresh breakout re-arms** (Bank Nifty also fires **14:21**
      from an earlier breakout). **Stop = the session low so far** (day high for shorts; Bank Nifty
      57464). `vote_breakout_pullback` (`indicators/directional.py`) rewritten to this state machine
      (mirror short: low crosses lower band, below 45-EMA → first close ABOVE the 5-EMA; close
      through the 45-EMA cancels; emits isolated +1/-1 so flip-detection = one trigger/pullback).
      Stop wired via `feeds.snapshot._chart_read` (session_low/high in levels) → `analysis.trade1.
      trade1_levels` (session-extreme stop, structure fallback) → `analysis.triggers` (running
      session low/high per trigger, new `_session_extremes`). `journal_trigger_config`
      (`voters=["breakout_pullback"]`, confirm 0) + web/training wiring unchanged → live + training
      flip automatically. Harness `--candidates` rewritten; validated Nifty 14:18, Bank Nifty 14:21
      + 14:39 (14:39 armed by the fresh 14:30 breach). The VRL is demoted to context. WHICH pullback
      to act on (14:21 vs 14:39) + target/trailing = the Phase-2 Claude agent's learned job. Docs
      updated (config.example, DIRECTIONAL_SPEC, JOURNAL_EXTRACTION). Tested in test_directional /
      test_trigger_check / test_triggers / test_training (session-low stop) + suite green (156).
  - [x] **Phase 2 Slice 1 — take/skip + P&L + "reason why" learning loop (genuine/false labels).**
    Confirmed with the trader: take/skip + evaluate P&L + **find the reason why**, labelled in **both**
    training replay and live. New `agent/reason.py` `explain_outcome` (injectable completer like
    `claude_read`; `ReasonWhy` = why-won/lost + `trigger_quality` genuine/false + lesson, graded by
    PROCESS not P&L). `journal/store.py` gained `trigger_label` + `reason_why` columns (migrated) +
    `update_reason` (live settle patches the reason post-outcome). Web: `REASON_COMPLETER` seam;
    `/api/train/answer` takes a `label` + runs the post-mortem on the trader's executed levels (stored
    on the training row + returned for the reveal); `/api/decision` carries the live `label`;
    `/api/record` runs `_settle_reasons` (one post-mortem per newly-resolved LIVE trade) + returns
    recent `posts`. `agent/memory.distill_context` now surfaces the trader's genuine/false labels +
    Claude's post-mortems so take/skip sharpens against ground truth. Frontend: genuine/false radios +
    post-mortem panel in `/train` (reveal) and the live cockpit (`recPosts`). Tested in
    test_agent_reason / test_journal_store / test_training / test_web_server + suite green (163).
  - **PENDING — Phase 2 Slice 2: dynamic level management.** Once entered, Claude proposes + **trails**
    target/stop/**TSL** as price moves (propose-only). Open: cadence (per-bar vs on-move), TSL basis
    (5-EMA/Supertrend/swing), cockpit surfacing.
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
- [ ] **Refine the OI-confluence boost (next step, confirmed with user):** make the +1 a
      WEIGHTED bump instead of flat — strong chain agreement (e.g. clear PCR + max-pain +
      wall alignment) → +2, marginal lean → +1, conflicting OI → optional −1. Drive it off the
      strength of Claude's `oi_bias` / the raw OI fields, still capped at the band top.
      Also retune the strike agent's ~25-pt time-value cutoff (consider a %-of-premium variant)
      once watched live. (`analysis/trade1.apply_oi_boost`, `analysis/strike.select_strike`.)
- [ ] Confirm Breeze expiry weekday live (TUESDAY=1) + GIFT/macro source; Twelve
      Data free tier lacks indices/commodities (USD/INR works).
- [ ] Phase 4/5 (CONTEXT) — harden Breeze live order path; port to more instruments.
- [ ] Phase 3 — Trade 2/3 buckets + Stage-2 levels (real calibration).
- [ ] Phase 4 — harden Breeze live order path + journal/grading loop.
- [ ] Stage 2 (levels: entry/stop/target, R-multiple) on Stage-1 survivors.
