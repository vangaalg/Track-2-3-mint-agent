"""Trade 1 — the directional bucket (EMA-anchored, deep-ITM/ATM delta vehicle).

Machine A gives the read (the MTF long/short/flat call); Machine B places the
levels (entry / stop / target / size / vehicle) off the chart structure and the
OI walls, then the discipline gate decides ENTER vs STAND_DOWN. Level heuristics
here are a deliberate FIRST CUT — Stage-2 calibration (CONTEXT) refines them.

Per the journal's Trade-1 rulebook:
  * entry  — at the read's anchor (here: current spot, the EMA-anchored trigger)
  * stop   — structure: nearest of 45-EMA / Supertrend / CPR band beyond price
  * target — next CPR level or OI wall in the trade's direction, else R-multiple
  * size   — normal 65-130 lots (default 75)
  * vehicle— a deep-ITM option (~0.8-1.0 delta), the trader's ₹600-700 signature
"""

from __future__ import annotations

from analysis.proposal import TradeProposal, Recommendation
from analysis import discipline

# Nifty contract + first-cut level params (PROVISIONAL — Stage-2 calibrates).
LOT_SIZE = 75
DEFAULT_SIZE_LOTS = 75
R_MULTIPLE = 1.5           # fallback reward:risk when no structural target exists
ITM_OFFSET = 300           # points in-the-money for the deep-ITM strike
STRIKE_STEP = 50


def _nearest_below(spot: float, levels: list[float]) -> float | None:
    below = [x for x in levels if x is not None and x < spot]
    return max(below) if below else None


def _nearest_above(spot: float, levels: list[float]) -> float | None:
    above = [x for x in levels if x is not None and x > spot]
    return min(above) if above else None


def _round_strike(x: float) -> int:
    return int(round(x / STRIKE_STEP) * STRIKE_STEP)


def propose_trade1(snapshot, size_lots: int = DEFAULT_SIZE_LOTS) -> TradeProposal:
    """Build a Trade-1 proposal from a ``feeds.snapshot.Snapshot``."""
    read = snapshot.chart_read
    direction = read["mtf_call"]
    spot = snapshot.spot
    lv = read["levels"]
    oi = snapshot.oi or {}

    base = dict(
        instrument=snapshot.instrument, trade_type="trade1", ts=snapshot.ts,
        direction=direction, spot=spot,
        context={"chart_read": read, "oi": snapshot.oi, "macro": snapshot.macro,
                 "notes": snapshot.notes},
    )

    # Flat/conflicted read → STAND_DOWN immediately (no levels to place).
    if direction not in ("long", "short"):
        rec, reasons = discipline.evaluate({}, direction, size_lots)
        return TradeProposal(recommendation=rec, reasons=reasons, **base)

    long = direction == "long"
    supports = [lv.get("ema_45"), lv.get("supertrend"), lv.get("cpr_bc"), lv.get("cpr_pivot")]
    resists = [lv.get("cpr_tc"), lv.get("cpr_pivot")]
    if oi.get("call_wall"):
        resists.append(oi["call_wall"]["strike"])
    if oi.get("put_shelf"):
        supports.append(oi["put_shelf"]["strike"])

    entry = spot
    if long:
        stop = _nearest_below(spot, supports)
        target = _nearest_above(spot, resists)
    else:
        stop = _nearest_above(spot, resists)
        target = _nearest_below(spot, supports)

    # Fall back to an R-multiple target when no structural level lies ahead.
    risk_pts = abs(entry - stop) if stop is not None else None
    if target is None and risk_pts:
        target = entry + R_MULTIPLE * risk_pts * (1 if long else -1)

    reward_pts = abs(target - entry) if target is not None else None
    rupee_risk = round(risk_pts * LOT_SIZE * size_lots, 2) if risk_pts else None
    rr = round(reward_pts / risk_pts, 2) if (risk_pts and reward_pts) else None

    right = "CE" if long else "PE"
    strike = _round_strike(spot - ITM_OFFSET) if long else _round_strike(spot + ITM_OFFSET)
    vehicle = f"{snapshot.instrument} {strike} {right} (deep-ITM, ~0.8-1.0 delta)"

    checklist = {
        "edge": f"{direction} read confirmed by MTF stack (45-EMA regime + 3m trigger)",
        "stop": f"{stop:.2f} (structure)" if stop is not None else "",
        "size": f"{size_lots} lots (normal band)",
        "invalidation": (
            f"price closing {'below' if long else 'above'} {stop:.2f}"
            if stop is not None else ""
        ),
        "target": f"{target:.2f}" if target is not None else "",
        "time_container": "intraday — flat by close",
    }

    rec, reasons = discipline.evaluate(checklist, direction, size_lots)
    reasons = _explain(read, oi) + reasons

    return TradeProposal(
        entry=round(entry, 2),
        stop=round(stop, 2) if stop is not None else None,
        target=round(target, 2) if target is not None else None,
        size_lots=size_lots, vehicle=vehicle, rupee_risk=rupee_risk, rr_ratio=rr,
        recommendation=rec, checklist=checklist, reasons=reasons, **base,
    )


def _explain(read: dict, oi: dict) -> list[str]:
    out = [
        f"MTF call: {read['mtf_call']}; daily 45-EMA regime "
        f"{_word(read['regime_45_daily'])}; 3m Supertrend {_word(read['supertrend_3m'])}.",
    ]
    if oi:
        out.append(
            f"OI: PCR {oi.get('pcr'):.2f}; call wall {oi.get('call_wall', {}).get('strike')}, "
            f"put shelf {oi.get('put_shelf', {}).get('strike')}."
        )
    return out


def _word(sign: int) -> str:
    return {1: "up", -1: "down", 0: "flat"}.get(int(sign), "flat")
