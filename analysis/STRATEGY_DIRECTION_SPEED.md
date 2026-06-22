# Strategy spec — the two-read model (DIRECTION × SPEED → strike / buy-write)

> Status: **specification**, not a validated edge. This is the target the `option-trader` agent's
> logic and the `scoring.backtest --selection` rig build toward, once enough live OI/VIX/macro has
> accumulated. Every signal here must clear the **out-of-sample + cost** bar before it goes live —
> the discipline that caught three curve-fit traps (HTF filter, levels, all static price features).

## The correction that frames everything
The 3-min breakout-pullback trigger is **only an ATTENTION signal** ("look now"). Three years of
backtests proved it has **no standalone edge** (edge ratio ≈ 1.0; no fixed level clears costs; every
static price feature failed OOS). The edge is the **read the trader does at that moment** — a
two-part decision that picks the actual trade:

```
trigger (look) ─▶  DIRECTION read ─┐
                                   ├─▶  STRIKE (ITM/ATM/OTM/deep-ITM) + BUY-or-WRITE + timing
                   SPEED read    ──┘
```

## Read 1 — DIRECTION (up or down)
| Input | What it says |
|---|---|
| **OI walls** — highest call-OI strike = resistance, highest put-OI strike = support | the ceiling / floor price respects |
| **Distance to wall** + the trader's **±37 / ±72 extension bands** (NIFTY; scaled for others) | breach-out-then-revert geometry |
| **PCR** (put/call OI) | positioning lean |
| **max-pain** | expiry gravity |
| **Macro overlay** — USD-INR, **US30/Dow (overnight)**, Nasdaq, crude, **GIFT Nifty (gap)** | the regime / gap lean |
| **Overnight geopolitical events** | catalyst / regime shift (manual brief, see below) |

## Read 2 — SPEED (fast or slow → buy or write)
The trader's base read (VIX + distance-to-wall + event + expiry) is a sound *environment* gauge but
a weak *speed forecast* — it has no denominator. The `option-trader` agent's upgrades (all
computable from the 15-min capture):

1. **Expected move vs distance-to-target** *(highest value)*. The ATM straddle is the market's
   expected move: `EM ≈ ATM_call_LTP + ATM_put_LTP` (both already in `feeds.oi.chain_table`).
   Scale to the intraday horizon `× sqrt(time_left_today / time_to_expiry)`. Then
   `move_budget = distance_to_target / EM_intraday`:
   - `< ~0.6` → target reachable → **BUY** premium viable
   - `> ~1` → target beyond what's priced → long premium bleeds theta → **WRITE** / stand down
2. **Implied vs realized vol** = the **buy-vs-write coin**. IV from per-strike LTP (BS-inverse),
   realized vol from our own 1m/3m bars (annualized close-to-close stdev).
   - IV ≫ RV (premium rich, tape calmer than priced) → **WRITE**
   - IV ≪ RV (move underpriced) → **BUY**
3. **Wall OI build-up vs unwind** = **defended (slow → fade) vs breaking (fast → ride)**. A `.diff()`
   on the `call_wall_oi` / `put_shelf_oi` already persisted each cycle in `oi_summary_store`:
   - wall OI **rising** as price approaches → defenders holding → slow rejection → **WRITE / fade to strike**
   - wall OI **falling** (unwinding) as price pushes in → cap breaking → fast breakout → **BUY through**
4. **Context**: VIX (regime), time-to-expiry (theta acceleration, expiry-day = bimodal pin-vs-break),
   intraday time-of-day vol (open fast, midday lull, post-14:15 re-accelerate).
5. *(Tier 2, needs per-strike IV)* **IV skew** (put vs call IV → directional-speed) and **term
   structure** (front-expiry IV ≫ next = event priced into today).

## The decision table (BUY / WRITE / deep-ITM / STAND-DOWN)
| Read | Action |
|---|---|
| IV ≤ RV **and** move_budget < 0.6 **and** wall **unwinding** **and** not midday/pre-event | **BUY** premium (ATM/slightly-OTM, fast directional) |
| IV ≫ RV **and** price breached a band with wall OI **rising** **and** max-pain/PCR point back to strike | **WRITE** premium — **as a defined-risk spread**, never naked (intraday, ₹150 cost) |
| High directional conviction (OI+PCR+macro agree) but IV rich/late-day | **deep-ITM** directional (extrinsic ≲ 25 pts → high delta, ~0 theta — capture the move, skip the vol/theta bet) |
| move_budget > 1 **and** IV rich · or wall defended but PCR/max-pain disagree · or pre-event & not breaking · or gross edge < ~4–5 pts | **STAND DOWN** |

Unifying logic: **BUY when speed is cheap and the target is reachable; WRITE when speed is expensive
and price is pinned; go deep-ITM when you want the direction but not the vol/theta bet; stand down
when the market prices a move bigger than you can afford to wait for.**

## Data map — what feeds each signal
| Signal | Source | Status |
|---|---|---|
| OI walls / PCR / max-pain / ATM / spot | `feeds.oi.summarise_chain`, `oi_store`, `oi_summary_store` | ✅ recording |
| ±37/72 bands, distance | `feeds.oi_levels.wall_levels` | ✅ |
| VIX, USD-INR, Dow/US30, Nasdaq, crude | `feeds.macro` → `macro_store` | ✅ recording |
| **GIFT Nifty (gap)** | `feeds.gift` (investing.com, best-effort) + manual override | ✅ this change |
| **Overnight events** | Claude brief from a screenshot → saved as `events_note` (see below) | ✅ this change |
| ATM straddle / expected move, bands in EM units | `chain_table` (`call_ltp+put_ltp`) — division only | ⏳ compute when modelling |
| Wall OI Δ (defended/breaking) | `.diff()` on `oi_summary_store` columns | ⏳ when modelling |
| **Per-strike IV** (VRP, skew, term structure) | BS-inverse on per-strike LTP | ⏳ extra calc (deferrable via straddle proxy) |
| **Realized vol** | annualized close-to-close stdev on 1m/3m bars | ⏳ small calc |

## Overnight events — captured the way the trader already works
Not a news scraper. The trader shares a **GIFT Nifty / news screenshot** and Claude surfaces
"everything geopolitical since the last close" (the cockpit already has screenshot→Claude). That
brief is **saved as `events_note`** via the recorder's `POST /context`; GIFT Nifty is auto
(investing.com) with a **manual override as the source of truth**.

## Validation discipline (unchanged, non-negotiable)
No signal here is trusted until `scoring.backtest --selection` shows it **positive in every
out-of-sample period AND after ~₹150/round-trip costs**. The recorder is banking the inputs now;
the modelling + validation phase begins once weeks–months of intraday OI/VIX/macro have accrued.
