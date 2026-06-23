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
# LOT_SIZE = the NIFTY contract multiplier (trader-confirmed 65). Other instruments
# override it via feeds.instruments (e.g. Bank Nifty = 30), threaded by the cockpit.
LOT_SIZE = 65
DEFAULT_SIZE_LOTS = 75
R_MULTIPLE = 1.5           # MINIMUM reward:risk — structural targets are floored to this
ITM_OFFSET = 300           # points in-the-money for the deep-ITM strike
STRIKE_STEP = 50
SIZE_BAND = (65, 130)      # journal's normal lot band; MTF confidence picks within it


def size_for_confidence(conf: int, lo: int = SIZE_BAND[0], hi: int = SIZE_BAND[1],
                        levels: int = 5) -> int:
    """Map MTF 45-EMA confidence (0..levels) linearly across the lot band.

    conf 0 -> lo (least HTF agreement), conf == levels -> hi (full stack aligned).
    """
    conf = max(0, min(levels, int(conf)))
    return int(round(lo + (hi - lo) * conf / levels))


def apply_strike(prop: "TradeProposal", pick: dict | None) -> "TradeProposal":
    """Attach the LIVE strike-agent pick to a proposal and rewrite its vehicle string.

    ``pick`` is the dict from ``analysis.strike.select_strike`` (or None — no-op).
    """
    if not pick:
        return prop
    prop.selected_strike = pick["strike"]
    prop.vehicle_ltp = pick["ltp"]
    prop.vehicle_extrinsic = pick["extrinsic"]
    prop.vehicle = (f"{prop.instrument} {pick['strike']} {pick['right']} "
                    f"@{pick['ltp']:.0f} (ITM, time-value {pick['extrinsic']:.0f})")
    return prop


def _oi_agrees(oi_bias: str | None, direction: str) -> bool:
    return ((oi_bias == "bullish" and direction == "long")
            or (oi_bias == "bearish" and direction == "short"))


def apply_oi_boost(prop: "TradeProposal", oi_bias: str | None) -> "TradeProposal":
    """LIVE OI confluence: +1 conviction when Claude's chain lean agrees with the
    trade, re-nudging the size (capped at the band top). STAND_DOWN records the bias
    only. Idempotent — recomputes from ``mtf_confidence`` each call.
    """
    boost = 1 if _oi_agrees(oi_bias, prop.direction) else 0
    prop.oi_bias = oi_bias
    prop.oi_confidence_boost = boost
    final = min(5, int(prop.mtf_confidence or 0) + boost)
    prop.final_confidence = final
    if (prop.recommendation is Recommendation.ENTER
            and prop.entry is not None and prop.stop is not None):
        prop.size_lots = size_for_confidence(final)
        prop.rupee_risk = round(abs(prop.entry - prop.stop) * LOT_SIZE * prop.size_lots, 2)
    return prop


def _nearest_below(spot: float, levels: list[float]) -> float | None:
    below = [x for x in levels if x is not None and x < spot]
    return max(below) if below else None


def _nearest_above(spot: float, levels: list[float]) -> float | None:
    above = [x for x in levels if x is not None and x > spot]
    return min(above) if above else None


def _round_strike(x: float) -> int:
    return int(round(x / STRIKE_STEP) * STRIKE_STEP)


def trade1_levels(direction: str, entry: float, levels: dict, oi: dict | None = None,
                  target_driven: bool = False, min_stop: float = 0.0):
    """Place stop & target for a Trade-1 entry from chart structure (+ OI walls).

    Reusable by both ``propose_trade1`` (latest bar) and ``analysis.triggers``
    (per-bar replay). ``levels`` is the chart-read levels dict (ema_45, supertrend,
    cpr_*); ``oi`` optionally adds the call wall / put shelf.

    Two level models:
      * default (journal) — STOP-driven: stop = the session extreme (day low for a
        long), target = next structure ahead, floored to ``R_MULTIPLE``×risk.
      * ``target_driven=True`` — anchor on the structural OBJECTIVE ahead and derive
        the stop so reward:risk == ``R_MULTIPLE`` exactly. Fixes the SL off the target
        instead of gluing it to the session low (which caused fraction-of-a-point
        instant stop-outs). Falls back to the stop-driven model when nothing lies ahead.

    ``min_stop`` (points) floors the stop distance — when the structural levels put
    the stop a fraction of a point from entry (the tiny-trade degeneracy), the risk is
    widened to ``min_stop`` and, in target-driven mode, the target is pushed out to
    keep R:R. 0 = off.

    Returns ``(stop, target, rr)``.
    """
    oi = oi or {}
    long = direction == "long"
    supports = [levels.get("ema_45"), levels.get("supertrend"),
                levels.get("cpr_bc"), levels.get("cpr_pivot")]
    resists = [levels.get("cpr_tc"), levels.get("cpr_pivot")]
    if oi.get("call_wall"):
        resists.append(oi["call_wall"]["strike"])
    if oi.get("put_shelf"):
        supports.append(oi["put_shelf"]["strike"])

    if target_driven:
        # Target first: the next structural level ahead is the objective; the stop is
        # whatever keeps reward:risk at R_MULTIPLE (no session-low gluing).
        target = _nearest_above(entry, resists) if long else _nearest_below(entry, supports)
        if target is not None:
            reward = abs(target - entry)
            risk = reward / R_MULTIPLE
            if min_stop and risk < min_stop:        # floor the stop, push target to keep R:R
                risk = min_stop
                target = entry + R_MULTIPLE * risk * (1 if long else -1)
            stop = entry - risk if long else entry + risk
            return stop, target, R_MULTIPLE
        # nothing ahead -> fall through to the stop-driven model

    # The journal's stop = the session's running extreme (day low for a long, day
    # high for a short). Falls back to chart structure when it isn't supplied.
    sess_low, sess_high = levels.get("session_low"), levels.get("session_high")
    if long:
        stop = sess_low if (sess_low is not None and sess_low < entry) else _nearest_below(entry, supports)
        target = _nearest_above(entry, resists)
    else:
        stop = sess_high if (sess_high is not None and sess_high > entry) else _nearest_above(entry, resists)
        target = _nearest_below(entry, supports)

    risk = abs(entry - stop) if stop is not None else None
    if risk is not None and min_stop and risk < min_stop:   # floor a too-tight stop
        risk = min_stop
        stop = entry - risk if long else entry + risk
    # Enforce a MINIMUM reward:risk. A structural target closer than R_MULTIPLE×risk
    # (or none at all) is pushed out to the floor, so a "win" is always a real move
    # — kills the near-zero-point targets that made rr collapse toward 0.
    if risk:
        min_reward = R_MULTIPLE * risk
        if target is None or abs(target - entry) < min_reward:
            target = entry + min_reward * (1 if long else -1)
    reward = abs(target - entry) if target is not None else None
    rr = round(reward / risk, 2) if (risk and reward) else None
    return stop, target, rr


_EDGE = {
    "trade1": "MTF stack (45-EMA regime + 3m trigger)",
    "cpr_st": "CPR + Supertrend trend-rider (narrow-CPR day, 5-EMA pullback)",
    "orb": "Opening-Range Breakout confirmed by VWAP",
}


def build_directional_proposal(
    *, instrument: str, ts: str, spot: float, read: dict, oi: dict | None,
    macro: dict | None, notes, trade_type: str = "trade1",
    size_lots: int = DEFAULT_SIZE_LOTS, oi_levels: dict | None = None,
) -> TradeProposal:
    """Shared directional proposal builder (Trade-1 + the new chart strategies).

    ``read`` is a chart-read dict (``mtf_call`` + ``levels`` + ``mtf_confidence``) as
    produced by ``feeds.snapshot._chart_read`` for the strategy's resolver config.
    ``oi`` is carried for context/explanation; ``oi_levels`` (Trade-1 only) feeds the
    OI walls into level placement — the new strategies pass ``None`` (OI is evaluated
    MANUALLY by the trader, so it never auto-shapes their mechanical levels)."""
    direction = read["mtf_call"]
    lv = read["levels"]

    # MTF 45-EMA conviction scales the size across the journal band (65-130 lots).
    mtf_conf = read.get("mtf_confidence")
    if mtf_conf is not None:
        size_lots = size_for_confidence(mtf_conf)

    base = dict(
        instrument=instrument, trade_type=trade_type, ts=ts,
        direction=direction, spot=spot, mtf_confidence=int(mtf_conf or 0),
        context={"chart_read": read, "oi": oi, "macro": macro, "notes": notes},
    )

    # Flat/conflicted read → STAND_DOWN immediately (no levels to place).
    if direction not in ("long", "short"):
        rec, reasons = discipline.evaluate({}, direction, size_lots)
        return TradeProposal(recommendation=rec, reasons=reasons, **base)

    long = direction == "long"
    entry = spot
    stop, target, rr = trade1_levels(direction, entry, lv, oi_levels)

    risk_pts = abs(entry - stop) if stop is not None else None
    rupee_risk = round(risk_pts * LOT_SIZE * size_lots, 2) if risk_pts else None

    right = "CE" if long else "PE"
    strike = _round_strike(spot - ITM_OFFSET) if long else _round_strike(spot + ITM_OFFSET)
    vehicle = f"{instrument} {strike} {right} (deep-ITM, ~0.8-1.0 delta)"

    checklist = {
        "edge": f"{direction} read confirmed by {_EDGE.get(trade_type, 'the chart stack')}",
        "stop": f"{stop:.2f} (structure)" if stop is not None else "",
        "size": (f"{size_lots} lots (MTF conf {int(mtf_conf or 0)}/5, band "
                 f"{SIZE_BAND[0]}-{SIZE_BAND[1]})" if mtf_conf is not None
                 else f"{size_lots} lots (normal band)"),
        "invalidation": (
            f"price closing {'below' if long else 'above'} {stop:.2f}"
            if stop is not None else ""
        ),
        "target": f"{target:.2f}" if target is not None else "",
        "time_container": "intraday — flat by close",
    }

    rec, reasons = discipline.evaluate(checklist, direction, size_lots)
    reasons = _explain(read, oi_levels or {}) + reasons

    return TradeProposal(
        entry=round(entry, 2),
        stop=round(stop, 2) if stop is not None else None,
        target=round(target, 2) if target is not None else None,
        size_lots=size_lots, vehicle=vehicle, rupee_risk=rupee_risk, rr_ratio=rr,
        recommendation=rec, checklist=checklist, reasons=reasons, **base,
    )


def propose_trade1(snapshot, size_lots: int = DEFAULT_SIZE_LOTS) -> TradeProposal:
    """Build a Trade-1 proposal from a ``feeds.snapshot.Snapshot`` (OI feeds levels)."""
    return build_directional_proposal(
        instrument=snapshot.instrument, ts=snapshot.ts, spot=snapshot.spot,
        read=snapshot.chart_read, oi=snapshot.oi, macro=snapshot.macro,
        notes=snapshot.notes, trade_type="trade1", size_lots=size_lots,
        oi_levels=snapshot.oi,
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
