"""Intraday signal replay — "what if I'd traded today's Trade-1 triggers".

The MTF resolver already produces a long/short/flat call on *every* 3-min bar
(``resolve_direction_mtf``). A **trigger** is the bar where that call flips INTO a
side (a new entry). For each trigger we place chart-structure stop/target
(``analysis.trade1.trade1_levels``) and simulate forward over the day's 3-min bars
until the first of stop/target is touched (else mark-to-close). The result is a
list of today's hypothetical trades + a P&L summary — the dashboard's trigger view.

OI is current-only, so the replay is chart-based (no per-bar walls). Pure and
testable: pass already-built feature/OHLCV frames.
"""

from __future__ import annotations

import pandas as pd

from indicators.directional import MTFDirectionalConfig, resolve_direction_mtf
from analysis.trade1 import trade1_levels, LOT_SIZE, DEFAULT_SIZE_LOTS


def _f(x):
    try:
        return None if pd.isna(x) else float(x)
    except (TypeError, ValueError):
        return None


def simulate_trade(direction, entry, stop, target, highs, lows, close_last):
    """Walk forward bars and resolve a trade to the first of stop/target.

    If a bar touches both, the stop wins (conservative). If neither is touched,
    the trade is left ``open`` and marked to ``close_last``. Returns
    ``(outcome, exit_px, points)`` where outcome ∈ win/loss/open and points are
    signed in the trade's favour.
    """
    long = direction == "long"
    outcome, exit_px = "open", float(close_last)
    for h, lo in zip(highs, lows):
        if long and lo <= stop:
            outcome, exit_px = "loss", stop; break
        if long and h >= target:
            outcome, exit_px = "win", target; break
        if (not long) and h >= stop:
            outcome, exit_px = "loss", stop; break
        if (not long) and lo <= target:
            outcome, exit_px = "win", target; break
    points = (exit_px - entry) if long else (entry - exit_px)
    return outcome, round(exit_px, 2), round(points, 2)


def replay_today(
    feats_by_tf: dict[str, pd.DataFrame],
    frames_by_tf: dict[str, pd.DataFrame],
    cfg: MTFDirectionalConfig | None = None,
    size_lots: int = DEFAULT_SIZE_LOTS,
    lot_size: int = LOT_SIZE,
) -> dict:
    """Replay the latest session's Trade-1 triggers and their outcomes."""
    cfg = cfg or MTFDirectionalConfig()
    empty = {"session": None, "triggers": [], "last": None,
             "summary": {"n": 0, "wins": 0, "losses": 0, "open": 0,
                         "net_points": 0.0, "net_rupees": 0.0, "hit_rate": None}}
    if "3min" not in feats_by_tf or "3min" not in frames_by_tf:
        return empty

    calls = resolve_direction_mtf(feats_by_tf, cfg)
    if calls.empty:
        return empty

    today = calls.index[-1].date()
    in_day = pd.Index([ts.date() == today for ts in calls.index])
    calls = calls[in_day.values]
    bars = frames_by_tf["3min"].reindex(calls.index)
    feats = feats_by_tf["3min"].reindex(calls.index)
    if len(calls) < 2:
        return {**empty, "session": str(today)}

    c = calls.to_numpy()
    high, low, close = bars["high"].to_numpy(), bars["low"].to_numpy(), bars["close"].to_numpy()
    ts = calls.index

    triggers = []
    for i in range(len(c)):
        prev = c[i - 1] if i > 0 else "flat"
        if c[i] not in ("long", "short") or c[i] == prev:
            continue
        direction, entry = c[i], float(close[i])
        row = feats.iloc[i]
        levels = {k: _f(row.get(k)) for k in
                  ("ema_45", "supertrend", "cpr_pivot", "cpr_tc", "cpr_bc")}
        stop, target, rr = trade1_levels(direction, entry, levels)
        if stop is None or target is None:
            continue

        outcome, exit_px, points = simulate_trade(
            direction, entry, stop, target, high[i + 1:], low[i + 1:], close[-1])
        triggers.append({
            "ts": ts[i].isoformat(), "direction": direction,
            "entry": round(entry, 2), "stop": round(stop, 2), "target": round(target, 2),
            "rr": rr, "outcome": outcome, "points": round(points, 2),
            "rupees": round(points * lot_size * size_lots, 0),
        })

    wins = sum(1 for t in triggers if t["outcome"] == "win")
    losses = sum(1 for t in triggers if t["outcome"] == "loss")
    opens = sum(1 for t in triggers if t["outcome"] == "open")
    net_pts = round(sum(t["points"] for t in triggers), 2)
    return {
        "session": str(today),
        "triggers": triggers,
        "last": triggers[-1] if triggers else None,
        "summary": {
            "n": len(triggers), "wins": wins, "losses": losses, "open": opens,
            "net_points": net_pts, "net_rupees": round(net_pts * lot_size * size_lots, 0),
            "hit_rate": round(wins / (wins + losses), 2) if (wins + losses) else None,
        },
    }
