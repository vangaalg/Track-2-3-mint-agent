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
| `ema` | fast EMA vs slow EMA cross | — |
| `macd` | histogram sign | — |
| `rsi` | `momentum`: >50 long / <50 short | `reversion`: <30 long / >70 short |
| `bollinger` | `reversion`: below lower long / above upper short | `breakout`: opposite |
| `three_min` | sign of summed 3-min component signals | — |

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
