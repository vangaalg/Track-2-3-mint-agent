"""Self-managed exits — pure decision core (the monitor loop lives in the cockpit).

Nothing rests at the broker. For each open live position the cockpit polls the
underlying price and calls ``evaluate_exit`` to decide: keep holding, trail the
stop up, or fire a MARKET exit (stop / target / trailing-stop hit). SL, target and
the trailing stop are all evaluated on the UNDERLYING price — the option vehicle is
always a long buy, so its risk is governed by where the index/share goes, exactly
like ``analysis.triggers.simulate_intraday`` / ``web.server._record_exit`` already work.

OCO + one-position are structural: there is at most ONE open position per
(symbol, strategy), so when we exit there is no sibling order to cancel — OCO is
automatic. The trailing-stop unifies with the hard stop (a trailed stop simply
raises ``stop``), so the stop-hit branch covers both.

``tsl_basis`` is parametric: ``"fixed"`` (a points trail) is implemented now;
``"ema5"`` / ``"supertrend"`` are reserved for a future indicator-based trail.
"""

from __future__ import annotations


def evaluate_exit(position: dict, price: float) -> dict:
    """Decide what to do with one open position at ``price`` (the underlying).

    Returns ``{"action": "hold"|"update"|"exit", "reason": str|None,
    "new_stop": float|None}``. ``update`` carries a raised trailing stop;
    ``exit`` carries why ("stop" | "target" | "tsl").
    """
    hold = {"action": "hold", "reason": None, "new_stop": None}

    # Never act before the entry is confirmed filled — a pending/rejected entry
    # must never trigger an exit against a position we don't actually hold.
    if not position.get("entry_filled"):
        return hold
    if price is None:
        return hold

    direction = position.get("direction")
    stop = position.get("stop")
    target = position.get("target")
    tsl = position.get("tsl_points")
    long = direction == "long"

    # 1) Trail: ratchet the stop toward price by tsl_points off the best excursion.
    if tsl:
        peak = position.get("peak")
        peak = price if peak is None else (max(peak, price) if long else min(peak, price))
        position["peak"] = peak                       # caller persists the mutated peak
        trailed = peak - tsl if long else peak + tsl
        # Only ever tighten (raise for long / lower for short), never loosen.
        if stop is None or (trailed > stop if long else trailed < stop):
            return {"action": "update", "reason": "tsl", "new_stop": round(trailed, 2)}

    # 2) Stop hit (covers the original SL and any trailed stop).
    if stop is not None and (price <= stop if long else price >= stop):
        return {"action": "exit", "reason": "stop", "new_stop": None}

    # 3) Target hit.
    if target is not None and (price >= target if long else price <= target):
        return {"action": "exit", "reason": "target", "new_stop": None}

    return hold
