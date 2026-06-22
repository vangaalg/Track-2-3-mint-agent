"""Expiry max-pain Iron Condor / Iron Fly — a NON-directional, defined-risk strategy.

The fourth alert stream and the only premium-seller: on an expiry day, when the tape
is range-bound and pinned inside the OI walls (regime gate =
``indicators.directional.vote_iron_condor_regime``), SELL the expected move as a
*defined-risk* iron condor (or iron fly) and let theta + the max-pain pin pay into the
close. Risk is capped by the long wings (``max_loss = wing_width − net_credit``), known
at entry — unlike a naked strangle.

Two pure helpers carry the math (``condor_legs`` builds the 4 legs + credit/
breakevens/max-loss off a live per-strike chain; ``condor_payoff`` is the expiry
payoff at a settle price). ``propose_condor`` is propose-only (no multi-leg execution
path — the trader places the legs manually). ``list_condor_triggers`` enumerates the
gated expiry-day setups and simulates the UNDERLYING staying-in-range outcome for the
backtest rig; because historical intraday option prices aren't available, it uses an
ATR-scaled expected move + a parametric credit — a REGIME-selection proxy, not a true
option backtest (no edge claimed; validate on ``scoring.backtest --strategy condor``).
"""

from __future__ import annotations

import pandas as pd

from analysis.proposal import TradeProposal, Recommendation
from analysis import discipline
from analysis.trade1 import STRIKE_STEP, LOT_SIZE, DEFAULT_SIZE_LOTS

STOP_MULT = 1.75          # exit if combined premium expands to ~1.5-2x the credit taken
TARGET_KEEP = 0.55        # bank ~50-60% of the credit (theta) / pin to close


def _round_step(x: float, step: int = STRIKE_STEP) -> int:
    return int(round(x / step) * step)


def _ltp(table: pd.DataFrame, strike: int, col: str) -> float | None:
    row = table[table["strike"] == strike]
    if row.empty:
        return None
    v = row.iloc[0].get(col)
    try:
        return float(v) if pd.notna(v) else None
    except (TypeError, ValueError):
        return None


def expected_move(table: pd.DataFrame, spot: float) -> float | None:
    """ATM-straddle expected move = ``call_ltp + put_ltp`` at the nearest strike."""
    if table is None or table.empty:
        return None
    atm = int(table["strike"].iloc[(table["strike"] - spot).abs().argmin()])
    c, p = _ltp(table, atm, "call_ltp"), _ltp(table, atm, "put_ltp")
    return None if c is None or p is None else round(c + p, 2)


def condor_legs(spot: float, table: pd.DataFrame, oi: dict | None = None,
                em: float | None = None, wing_width: int = STRIKE_STEP,
                mode: str = "condor") -> dict | None:
    """Build an iron condor / iron fly and its credit, breakevens and max-loss.

    Short strikes sit at the OI walls (``oi['call_wall']`` / ``oi['put_shelf']`` — the
    trader's manual walls) when given, else at ``spot ± expected_move`` (``mode='fly'``
    uses the ATM strike for both shorts). Long wings are ``wing_width`` further out, so
    the loss is defined. Returns the legs + ``net_credit``, two breakevens, ``max_loss
    = wing_width − net_credit`` and the management premia, or ``None`` when the chain
    can't price all four legs.
    """
    if table is None or table.empty:
        return None
    atm = int(table["strike"].iloc[(table["strike"] - spot).abs().argmin()])
    if em is None:
        em = expected_move(table, spot)
    if mode == "fly":
        short_call = short_put = atm
    elif oi and oi.get("call_wall") and oi.get("put_shelf"):
        short_call = _round_step(oi["call_wall"]["strike"])
        short_put = _round_step(oi["put_shelf"]["strike"])
    elif em:
        short_call = _round_step(spot + em)
        short_put = _round_step(spot - em)
    else:
        return None
    if short_call <= short_put:                      # degenerate (too-tight em) → bail
        short_call, short_put = atm + wing_width, atm - wing_width
    long_call, long_put = short_call + wing_width, short_put - wing_width

    sc, sp = _ltp(table, short_call, "call_ltp"), _ltp(table, short_put, "put_ltp")
    lc, lp = _ltp(table, long_call, "call_ltp"), _ltp(table, long_put, "put_ltp")
    if None in (sc, sp, lc, lp):
        return None
    net_credit = round(sc + sp - lc - lp, 2)
    if net_credit <= 0:
        return None
    return {
        "mode": mode, "wing_width": wing_width, "expected_move": em,
        "short_call": short_call, "short_put": short_put,
        "long_call": long_call, "long_put": long_put,
        "net_credit": net_credit,
        "be_low": round(short_put - net_credit, 2),
        "be_high": round(short_call + net_credit, 2),
        "max_loss": round(wing_width - net_credit, 2),
        "max_profit": net_credit,
        "stop_premium": round(STOP_MULT * net_credit, 2),
        "target_premium": round(TARGET_KEEP * net_credit, 2),
    }


def condor_payoff(legs: dict, settle: float) -> float:
    """Expiry payoff (points of premium, per lot) of the condor at ``settle`` price.

    ``net_credit`` between the shorts; each short spread caps its loss at the wing
    width. Max profit = ``net_credit`` (price pins between the shorts), max loss =
    ``net_credit − wing_width`` (a short spread fully breached)."""
    w, credit = legs["wing_width"], legs["net_credit"]
    call_loss = min(max(settle - legs["short_call"], 0.0), w)
    put_loss = min(max(legs["short_put"] - settle, 0.0), w)
    return round(credit - call_loss - put_loss, 2)


def _gate_open(snapshot, expiry_weekday: int = 1) -> bool:
    from indicators.directional import vote_iron_condor_regime
    feats3 = (snapshot.feats or {}).get("3min")
    if feats3 is None or feats3.empty:
        return False
    return bool(vote_iron_condor_regime(feats3, expiry_weekday=expiry_weekday).iloc[-1] == 1)


def propose_condor(snapshot, table: pd.DataFrame | None = None,
                   expiry_weekday: int = 1, size_lots: int = DEFAULT_SIZE_LOTS,
                   wing_width: int = STRIKE_STEP, mode: str = "condor") -> TradeProposal:
    """Build a propose-only iron-condor proposal from a snapshot + live per-strike table.

    STAND_DOWN unless the expiry-day range gate is open AND the live chain can price the
    four legs. The condor is NON-directional (``direction='flat'``); the scalar
    proposal fields carry the premium view (entry = net credit, stop = stop premium,
    target = target premium) and the full leg structure lives in ``context['legs']``."""
    base = dict(instrument=snapshot.instrument, trade_type="trade_condor",
                ts=snapshot.ts, direction="flat", spot=snapshot.spot,
                context={"chart_read": snapshot.chart_read, "oi": snapshot.oi,
                         "macro": snapshot.macro, "notes": snapshot.notes})

    if not _gate_open(snapshot, expiry_weekday):
        rec, reasons = discipline.evaluate({}, "flat", size_lots)
        reasons = ["Condor gate closed: needs an expiry-day range setup "
                   "(squeeze + inside CPR, after 11:00 IST)."] + reasons
        return TradeProposal(recommendation=rec, reasons=reasons, **base)

    legs = condor_legs(snapshot.spot, table, oi=snapshot.oi, wing_width=wing_width, mode=mode)
    if legs is None:
        rec, reasons = discipline.evaluate({}, "flat", size_lots)
        reasons = ["Condor gate open but the live chain can't price the four legs."] + reasons
        return TradeProposal(recommendation=rec, reasons=reasons, **base)

    base["context"]["legs"] = legs
    vehicle = (f"{snapshot.instrument} Iron {'Fly' if mode == 'fly' else 'Condor'}: "
               f"-{legs['short_put']}PE/-{legs['short_call']}CE, "
               f"+{legs['long_put']}PE/+{legs['long_call']}CE "
               f"(credit {legs['net_credit']:.1f}, max-loss {legs['max_loss']:.1f})")
    checklist = {
        "edge": "expiry-day range/pin: sell the expected move, theta + max-pain pay",
        "stop": f"buy back if premium ≥ {legs['stop_premium']:.1f} or a short strike breaks",
        "size": f"{size_lots} lots (defined risk; max-loss {legs['max_loss']:.1f} pts/lot)",
        "invalidation": f"close outside {legs['short_put']}-{legs['short_call']} "
                        f"(breakevens {legs['be_low']:.0f}/{legs['be_high']:.0f})",
        "target": f"bank ~{int(TARGET_KEEP * 100)}% credit (premium ≤ {legs['target_premium']:.1f}) / pin to close",
        "time_container": "intraday — flat by close (expiry)",
    }
    rec, reasons = discipline.evaluate(checklist, "long", size_lots)  # gate as a filled checklist
    reasons = [f"Iron condor: net credit {legs['net_credit']:.1f}, breakevens "
               f"{legs['be_low']:.0f}/{legs['be_high']:.0f}, max-loss {legs['max_loss']:.1f}."] + reasons
    rupee_risk = round(legs["max_loss"] * LOT_SIZE * size_lots, 2)
    return TradeProposal(
        entry=legs["net_credit"], stop=legs["stop_premium"], target=legs["target_premium"],
        size_lots=size_lots, vehicle=vehicle, rupee_risk=rupee_risk,
        rr_ratio=round(legs["max_profit"] / legs["max_loss"], 2) if legs["max_loss"] else None,
        recommendation=rec, checklist=checklist, reasons=reasons, **base,
    )


def list_condor_triggers(
    feats3m: pd.DataFrame, frame3m: pd.DataFrame, expiry_weekday: int = 1,
    em_mult: float = 1.0, atr_period: int = 14, wing_width: int = STRIKE_STEP,
    credit_frac: float = 0.35, size_lots: int = 1, lot_size: int = LOT_SIZE,
) -> list[dict]:
    """Enumerate gated expiry-day condor setups + their staying-in-range outcome.

    One setup per gated session (the condor is one trade/day): at the first bar the
    regime gate opens, place shorts at ``spot ± em_mult×ATR`` and walk the rest of the
    session — a breach of either short = LOSS (``-(wing − credit)``), otherwise the
    pin holds to the close = WIN (``+credit``), with ``credit = credit_frac × wing``.
    This is a REGIME-SELECTION proxy (no historical option prices); it measures whether
    the gate picks genuine range days. Returns ``aggregate``-compatible trigger dicts.
    """
    from indicators.directional import vote_iron_condor_regime
    from indicators.engine import atr as _atr

    if feats3m is None or feats3m.empty or frame3m is None or frame3m.empty:
        return []
    gate = vote_iron_condor_regime(feats3m, expiry_weekday=expiry_weekday).reindex(frame3m.index).fillna(0)
    atr = _atr(frame3m, atr_period)
    close = frame3m["close"]
    credit = round(credit_frac * wing_width, 2)
    loss_pts = round(wing_width - credit, 2)

    out: list[dict] = []
    seen_days: set = set()
    idx = frame3m.index
    for i in range(len(idx)):
        if gate.iloc[i] != 1:
            continue
        day = idx[i].normalize()
        if day in seen_days:
            continue
        seen_days.add(day)
        a = atr.iloc[i]
        if pd.isna(a) or a <= 0:
            continue
        spot = float(close.iloc[i])
        em = em_mult * float(a)
        short_call, short_put = spot + em, spot - em
        sess = frame3m[(frame3m.index > idx[i]) & (frame3m.index.normalize() == day)]
        breached = (not sess.empty) and (
            (sess["high"] >= short_call).any() or (sess["low"] <= short_put).any())
        outcome = "loss" if breached else "win"          # win = the pin held to the close
        points = -loss_pts if breached else credit
        out.append({
            "tid": len(out), "ts": idx[i].isoformat(), "date": str(idx[i].date()),
            "direction": "flat", "entry": round(spot, 2),
            "short_call": round(short_call, 2), "short_put": round(short_put, 2),
            "wing_width": wing_width, "credit": credit,
            "outcome": outcome, "points": round(points, 2),
            "rupees": round(points * lot_size * size_lots, 0),
        })
    return out
