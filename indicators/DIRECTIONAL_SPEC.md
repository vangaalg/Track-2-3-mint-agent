# Directional-Output Spec

How the chart stack collapses many (sometimes-disagreeing) indicators into a
**single per-bar call**: `long`, `short`, or `flat`. This is the heart of
Stage 1. Companion implementation: [`directional.py`](directional.py).

## The rule we must honour

> The chart stack's indicators won't always agree (EMA up, RSI overbought,
> Bollinger mid-band). The single long/short/flat call must resolve this. **Do
> NOT hardcode one method.** Implement BOTH as a config flag so Stage 1 scoring
> can empirically test which performs better, *per instrument*. Let the
> backtest decide; don't pre-commit.

So the resolver is a **switch**, not a fixed algorithm. Two methods, plus enough
knobs to express a hybrid.

## Vote convention

Every indicator is wrapped in a **voter** that emits, per bar, one of:

| value | meaning |
|------:|---------|
| `+1`  | long    |
|  `0`  | flat / abstain |
| `-1`  | short   |

Voters are pure functions of the indicator columns produced by
`engine.compute_indicators`. They live in a **registry** (`VOTERS`) keyed by
name, so the active indicator set and each voter's interpretation are
data-driven config, not code edits.

Built-in voters (all interpretations configurable, because e.g. "RSI as
momentum vs mean-reversion" is itself an empirical question):

| voter | default reading | alt mode |
|-------|-----------------|----------|
| `ema` | fast EMA (5) vs slow EMA (45) cross | — |
| `ema_stack` | full 5/45/100/200 ribbon aligned up/down | — |
| `regime_45` | close vs the 45-EMA (the journal's master regime filter) | — |
| `ema5_trigger` | close holding above/below the 5-EMA (3-min entry trigger) | — |
| `supertrend` | Supertrend direction | — |
| `cpr` | close vs CPR top/bottom central (daily/weekly bias) | — |
| `macd` | histogram sign | — |
| `rsi` | `momentum`: >50 long / <50 short | `reversion`: <30 long / >70 short |
| `bollinger` | `reversion`: below lower long / above upper short | `breakout`: opposite |
| `three_min` | sign of the journal trio (ema5_trigger + bb_vrl + 45-EMA pullback) — net-sign OR, so EMA-5 state alone fires; **over-triggers, experimental only** | — |
| `breakout_pullback` | **the trader's REAL 3-min entry (default):** the FIRST upper-band breach (`close > bb_upper`, above the 45-EMA) is the trigger and the **VRL = that breach bar's HIGH** (fixed). Price extends up, then retraces back to the VRL; the LONG fires on the bar where **`low ≤ VRL` (retest) AND `close > VRL` (VRL holds) AND `close < ema_5` (closed below the 5-EMA)**. The 5-EMA close is the discriminator (an earlier retest closing *above* the 5-EMA is not an entry). Mirror for short (first lower-band breach, VRL = its low, fire when `high ≥ VRL AND close < VRL AND close > ema_5`). A close through the 45-EMA cancels; latches flat after firing (one trigger per setup). Use `confirm_closes=0`. | — |
| `bb_reversal` | **SEPARATE squeeze-fade play (not the default):** a squeeze-gated Bollinger breach→**revert** whose close agrees with the EMA-5 side arms a direction, held while the EMA-5 holds, cleared on a flip. Fades the move; needs a prior coil. Pair with `confirm_closes=2`. | — |

**Confirmation gate.** `confirm_2_close(vote, df, n_closes=2, vol_window=20)`
wraps any vote with the journal's confirmation rule — keep a vote only where the
same sign has held `n_closes` consecutive bars **and** volume is expanding (with
a price-only fallback on zero-volume instruments). It is an opt-in transform, not
a voter. See [`JOURNAL_EXTRACTION.md`](JOURNAL_EXTRACTION.md).

## Method 1 — Confluence voting

N-of-M voters must agree, else flat.

```
longs   = count(votes == +1)
shorts  = count(votes == -1)
net     = longs - shorts
call    = long   if net >=  min_agree
          short  if net <= -min_agree
          flat   otherwise
```

`min_agree` is the agreement margin (e.g. with 6 voters, `min_agree = 4` ≈
"4-of-6 net"). Higher = more selective, more flat bars.

## Method 2 — Hierarchical

One **primary** voter decides direction; the others filter/veto.

```
direction = sign(primary_vote)            # the call, before gating
agree     = # of other voters agreeing with direction
opposite  = # of other voters opposing direction

take = direction != 0
if confirm_min > 0: take &= (agree >= confirm_min)   # confluence confirmation
if veto:            take &= (opposite == 0)          # hard veto

call = long/short per direction if take else flat
```

### Expressing the hybrid

The build note says the real method *"may turn out to be
hierarchical-with-confluence-confirmation."* That is exactly:

```
method      = hierarchical
primary     = <the deciding indicator>
confirm_min = k > 0      # primary decides, but ≥k others must confirm
veto        = true/false # optionally also let any opposer force flat
```

`confirm_min = 0, veto = true` → pure hierarchical (others only veto).
`confirm_min = k, veto = false` → primary + soft confluence confirmation.

## Config surface (`DirectionalConfig`)

| field | method | meaning |
|-------|--------|---------|
| `method` | both | `"confluence"` or `"hierarchical"` |
| `voters` | both | ordered list of active voter names |
| `voter_kwargs` | both | per-voter overrides, e.g. `{"rsi": {"mode": "reversion"}}` |
| `min_agree` | confluence | net-agreement margin to take a side |
| `primary` | hierarchical | the deciding voter |
| `confirm_min` | hierarchical | # of others that must confirm (0 = none) |
| `veto` | hierarchical | any opposer forces flat |

These map 1:1 to the `directional` block in `config.example.yaml`.

## Contract for Stage 1

`resolve_direction(df, cfg)` returns a `Series[str]` aligned to `df.index` with
values in `{long, short, flat}`. Stage 1 sweeps **both methods (and their
knobs)** per instrument and scores each — the winning resolver is an *output* of
the breadth test, never an assumption baked into the code.

---

## Multi-timeframe extension

The 3-min strategy is read **inside an MTF stack**, not on 3-min bars alone:

```
3m (trigger)  ·  15m  ·  60m  ·  daily  ·  weekly (regime)
```

Sourcing (rate-limit friendly): pull a **3m base** + **daily direct**; resample
15m/60m from the base and weekly from daily (`indicators/timeframes.py`).

### Two correctness rules

1. **Session anchoring** — intraday bins align to the market open (e.g. NSE
   09:15) via `resample_ohlcv(anchor=...)`, so a "15m bar" matches the chart.
2. **No lookahead** — a higher-TF bar is visible on the 3m timeline only after
   it has **closed**. `align_to_base` shifts each HTF bar to its close time
   (`open + rule`) then takes the last *completed* bar per 3m bar (`merge_asof`
   backward). At 3m bar *t* you see yesterday's completed daily, never today's
   still-forming one.

The single-TF resolver above runs **per timeframe**; voters become
*indicator × timeframe* once aligned.

### Three MTF combination methods (`mtf_method`)

| method | how timeframes combine |
|--------|------------------------|
| **`htf_bias_trigger`** *(default)* | Bias TFs (15m/60m/1d/1w) each resolve a call; their net agreement (≥ `bias_quorum`, `veto` cancels on conflict) sets a **bias**. The 3m call is the **trigger**. Take the trade only if trigger direction == bias and bias ≠ flat. Classic higher-TF-bias / lower-TF-entry. |
| **`cross_tf_confluence`** | Pool every (indicator × TF) vote, aligned to 3m, into one confluence count (`min_agree`). |
| **`per_tf_then_vote`** | Resolve a full call within each TF, then vote across the TF-level calls (net ≥ `bias_quorum`). |

### MTF config (`MTFDirectionalConfig`)

| field | meaning |
|-------|---------|
| `base` | the per-TF `DirectionalConfig` (voters/method/knobs), reused on each TF |
| `trigger_tf` | the entry timeframe (`3min`) |
| `bias_tfs` | higher timeframes that set/veto direction |
| `rules_by_tf` | pandas resample rule per non-trigger TF (for the close-time shift) |
| `mtf_method` | `htf_bias_trigger` \| `cross_tf_confluence` \| `per_tf_then_vote` |
| `bias_quorum` | net HTF agreement required to set a bias |
| `veto` | a conflicting HTF cancels the bias → stand down |

These map 1:1 to the `mtf` block in `config.example.yaml`. As with the single-TF
switch, **Stage 1 sweeps `mtf_method` per instrument** — the winning combination
is an output of the test, not a baked-in assumption. Grading is always on the
3m (trigger) timeline.
