# Journal → Chart-Layer Extraction Map

Maps the trader's live options journal (Sessions 001–010) onto **Track 2, the
chart-layer breadth test**. Track 2 is **chart layer ONLY** — per `CLAUDE.md`,
the OI/options layer, the macro layer, and the execution/discipline layer are
explicitly excluded and belong to the *separate strategy/execution repo*.

This doc exists so that (a) every chart-readable journal rule is implemented
here, and (b) nothing from the excluded layers leaks into this repo. Numeric
defaults introduced in code are **PROVISIONAL** — the same status the journal
gives its own thresholds ("0.9 / 1.1 / ~100 / ~150 are provisional until ~10
logged days validate them") — and are registered at the bottom.

---

## IN SCOPE — chart layer, implemented here

| Journal rule (trader's words) | Where it lives | Status |
|---|---|---|
| **45-EMA regime filter** — "danger while spot > 45-EMA; need closes below it" (the master rule, used on 45-week / 45-month / intraday) | `directional.vote_regime_45` (close vs `ema_45`) | ✅ |
| **3-min EMA-5 trigger** — "price holds above/below the 3-min EMA" | `engine.ema5_trigger` → `sig_ema5_trigger`; `directional.vote_ema5_trigger` | ✅ |
| **Confirmation** — "what confirms a signal? 2 closes + volume expanding + the stack agreeing — NOT a candle count" | `directional.confirm_2_close` (opt-in gate) | ✅ |
| **EMA ribbon stack** — EMA 5/45/100/200 alignment | `directional.vote_ema_stack`; `engine.compute_indicators` | ✅ |
| **Supertrend** — "weekly Supertrend bearish"; intraday Supertrend `(7,3)` | `engine.supertrend` (default 7/3, matching the chart); `directional.vote_supertrend` | ✅ |
| **CPR pivots** — the day's central-pivot range overlaid on every bar | `engine.cpr` (classic **daily** CPR broadcast onto all TFs, no-lookahead); `directional.vote_cpr` | ✅ |
| **Bollinger squeeze → VRL recovery** — "Bollinger crushed (~coil) → re-expands"; recovery back through a band edge | `engine.bollinger_vrl_breakout` (squeeze-gated) | ✅ |
| **MACD** — "hourly MACD expanding positive/negative"; "daily MACD positive" | `engine.macd`; `directional.vote_macd` | ✅ |
| **RSI not-extreme** — "RSI 81 overbought", "RSI 46 midline", "RSI 37.5" | `engine.rsi`; `directional.vote_rsi` (`momentum`/`reversion`) | ✅ |
| **45-EMA pullback continuation** — "buy the pullback in trend; sell strength into the falling 45-EMA" (staircase) | `engine.sma_pullback_continuation` (retargeted to 45-EMA) | ✅ |
| **Weight the 3-min signal by the weekly/monthly trend** — "establish weekly + monthly structure BEFORE acting on a 3-min signal" | MTF `htf_bias_trigger` (bias TFs gate the 3m trigger — `directional.resolve_direction_mtf`) | ✅ (existing) |
| **Step-0 gap-MAGNITUDE gate's chart half** — small gap → "read the levels (prior-day H/L, the actual walls), not the gap" | partially: prior-day levels are chart-readable; the *gap tree itself* is OUT (needs PCR/VIX/OI) | 🟡 partial — see OUT |

> The journal's "3-min strategy" is therefore the trio **EMA-5 trigger + Bollinger
> squeeze/VRL + 45-EMA pullback**. The old `ema_mean_reversion` stub is kept for
> experimentation but **excluded** — the trader is a documented trend-follower, not a
> mean-reverter.
>
> **Trigger correction #1 (over-fire).** `vote_three_min` aggregated the trio as
> `np.sign(sum)` — net-sign OR, so EMA-5 state alone fired and the signal over-triggered.
> First fix was `vote_bb_reversal` (a squeeze-gated breach→revert, EMA-5 confirmed).
>
> **Trigger correction #2 — the REAL strategy (confirmed against the trader's chart via
> `scoring/trigger_check.py`).** The harness on his 19-Jun export proved the direction was
> *backwards*: his 3-min entry is a **breakout CONTINUATION**, not a fade — but the entry is a
> **VRL retest**, not a bare 5-EMA touch (a v1 cut that fired the wrong bars and missed his
> trade). The mechanic he confirmed: the **FIRST upper-band breach** — the bar's **HIGH crosses
> the band** (`high > bb_upper`; the close may still be inside, e.g. Bank Nifty 13:42 high
> 57608.75 with its close below the band), above the 45-EMA — is the trigger and the
> **VRL = that breach bar's HIGH** (set once, fixed).
> Price extends up to a peak, then **retraces back DOWN to the VRL**; the LONG fires on the bar
> where **`low ≤ VRL` (retest) AND `close > VRL` (VRL holds) AND `close < ema_5` (closes below
> the 5-EMA)**. The **5-EMA close is the discriminator**: his 13:48 breach set VRL = 23962.65;
> 14:03 retested but closed *above* the 5-EMA (no entry); **14:18** retested (low 23962.45) and
> closed *below* the 5-EMA (23965.45 < 23975.58) = the real entry. Mirror for short (first
> lower-band breach, VRL = its low). Implemented as **`vote_breakout_pullback`**, the journal
> default (`journal_trigger_config`, `confirm_closes=0`). The **squeeze fade `vote_bb_reversal`
> is kept SEPARATE** (`squeeze_trigger_config`, `--strategy squeeze`) — it requires a coil and
> *fades* the poke. `vote_three_min` stays for experimentation. The remaining fuzzy edges (the
> `close>VRL` guard, touch vs close, the short side which the trader wants OI-gated) are left
> high-recall, to be learned by the Phase-2 Claude trigger agent rather than hardcoded.

---

## OUT OF SCOPE — belongs to the execution / strategy repo (NOT coded here)

| Journal content | Layer | Why it is excluded |
|---|---|---|
| **Gap decision tree** (continuation / pin / fade) + **Step-0 magnitude gate** | OI/options + macro | Drives the *vehicle/regime* call off PCR + VIX + walls — not chart-readable from OHLCV alone. |
| **Dial 1 — VIX direction** (energy gauge) | options/vol | No VIX series in the OHLCV chart layer. |
| **Dial 2 — PCR behaviour** (total vs ATM, trending vs parked) | OI/options | Option-chain data, not chart data. |
| **Dial 3 — OI-wall behaviour** (call wall / put shelf growing vs unwinding) | OI/options | Open-interest data; "wall as support/resistance" is an options-layer read. |
| **Vehicle map** (iron fly / condor / ITM-ATM delta / cheap-OTM bans) | options/execution | Instrument selection + expiry mechanics, not a directional chart read. |
| **Discipline contract** — 4-line / 6-line pre-trade check, "route the click through the check" | execution/discipline | Behavioural guardrail for the live agent, not a signal. |
| **Size discipline** — normal 65–130 lots, "I strongly believe = warning light", the 1,040 stand-down | execution/discipline | Position sizing / risk, not a chart feature. |
| **Scale-in template** (declared vs discovered martingale) | execution | Order-management protocol. |
| **Declaration protocol** + **bucket independence** | execution | Workflow between trader and the sparring agent. |
| **Macro scorecard** (GIFT, Brent, Dow, USD/INR, US VIX) + **Branch-1/2 weekly bias** | macro/geopolitical | Explicitly an excluded layer in `CLAUDE.md`. |
| **P&L grading matrix** (process vs outcome, the four boxes) | execution/meta | Trains the discipline layer, not the directional read. |
| **No-trade-is-a-win**, **regret-on-exit**, **capitulation-flip** lessons | execution/discipline | Behavioural; consumed by the journal/execution agent. |

> These are captured here only to mark the boundary. If/when the execution or
> options layers get their own repos, this table is the hand-off index.

---

## Provisional-thresholds register

Every numeric default introduced by this extraction. All **PROVISIONAL** — tune
against logged data, do not treat as the trader's calibrated edge.

| Constant | Default | Where | Journal basis |
|---|---|---|---|
| `confirm_2_close.n_closes` | `2` | `directional.confirm_2_close` | "2 closes" confirmation rule |
| `confirm_2_close.vol_window` | `20` | `directional.confirm_2_close` | "volume expanding" (no number given → 20-bar mean) |
| `bollinger_vrl_breakout.squeeze_window` | `50` | `engine.bollinger_vrl_breakout` | "Bollinger crushed (coil)" — trailing window for the squeeze percentile |
| `bollinger_vrl_breakout.squeeze_pct` | `0.25` | `engine.bollinger_vrl_breakout` | low-quantile = "crushed" width (no number given) |
| `sma_pullback_continuation.regime_period` | `45` | `engine.sma_pullback_continuation` | the 45-EMA master MA |
| `sma_pullback_continuation.trend_period` | `200` | `engine.sma_pullback_continuation` | long-trend EMA (200) |

> Inherited (registered last round, also provisional): EMA periods `5/45/100/200`,
> SMA `20`. **Supertrend `7 / 3.0`** and **CPR = daily-session broadcast** were
> pinned against the trader's real chart export (19 Jun 2026), not provisional.
> See `config.example.yaml` and `scoring/validate_export.py`.
