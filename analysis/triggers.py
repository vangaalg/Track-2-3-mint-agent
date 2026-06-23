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

from indicators.directional import (
    MTFDirectionalConfig, resolve_direction_mtf, mtf_ema45_confidence,
)
from indicators.engine import atr as _atr
from analysis.trade1 import trade1_levels, size_for_confidence, LOT_SIZE, DEFAULT_SIZE_LOTS


def _f(x):
    try:
        return None if pd.isna(x) else float(x)
    except (TypeError, ValueError):
        return None


def _session_extremes(frame3m: pd.DataFrame, ts) -> dict:
    """Running session low/high up to (and including) the entry bar ``ts`` — the
    journal's stop basis (day low for longs, day high for shorts)."""
    t = pd.Timestamp(ts)
    sess = frame3m[(frame3m.index <= t) & (frame3m.index.normalize() == t.normalize())]
    if sess.empty:
        return {}
    return {"session_low": _f(sess["low"].min()), "session_high": _f(sess["high"].max())}


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


def trigger_excursion(frame3m: pd.DataFrame, ts, direction: str, entry: float) -> tuple:
    """How far price travels AFTER a trigger, in points, over the rest of its session.

    Returns ``(mfe, mae, eod)`` where (all signed in the trigger's favour-frame):
      * ``mfe`` = max FAVOURABLE excursion — the furthest price ran the trigger's way
        (clamped at 0 if it never went favourable),
      * ``mae`` = max ADVERSE excursion — the worst heat against the trigger (≥0),
      * ``eod`` = signed points if held to the close (+ = profit).
    This is target-agnostic (no stop/target) — it measures the raw move the trigger
    predicts, so we can size targets off the real distribution instead of a guess.
    """
    t = pd.Timestamp(ts)
    sess = frame3m[(frame3m.index > t) & (frame3m.index.normalize() == t.normalize())]
    if sess.empty:
        return 0.0, 0.0, 0.0
    long = direction == "long"
    hi, lo = float(sess["high"].max()), float(sess["low"].min())
    close_last = float(sess["close"].iloc[-1])
    if long:
        mfe, mae, eod = hi - entry, entry - lo, close_last - entry
    else:
        mfe, mae, eod = entry - lo, hi - entry, entry - close_last
    return round(max(mfe, 0.0), 2), round(max(mae, 0.0), 2), round(eod, 2)


def simulate_intraday(frame3m: pd.DataFrame, ts, direction: str, entry: float,
                      stop: float, target: float) -> tuple[str, float, float]:
    """Resolve a trigger's outcome within its OWN session (Trade 1 is intraday).

    Walks the 3-min bars after ``ts`` up to that session's close and returns
    ``(outcome, exit_px, points)`` via ``simulate_trade`` (mark-to-close if neither
    stop nor target is touched by the bell).
    """
    t = pd.Timestamp(ts)
    sess = frame3m[(frame3m.index > t) & (frame3m.index.normalize() == t.normalize())]
    if sess.empty:
        return "open", round(float(entry), 2), 0.0
    return simulate_trade(direction, entry, stop, target,
                          sess["high"].to_numpy(), sess["low"].to_numpy(),
                          sess["close"].iloc[-1])


def _resolve_intraday(frame3m, ts, direction, entry, stop, target):
    """Like ``simulate_intraday`` but also returns the EXIT timestamp and labels a
    mark-to-close exit ``"eod"`` (Trade 1 is flat by the bell — a trigger that hits
    neither stop nor target is exited at the close, not left ``open``). Returns
    ``(outcome, exit_px, points, exit_ts)`` with outcome ∈ win/loss/eod.
    """
    t = pd.Timestamp(ts)
    sess = frame3m[(frame3m.index > t) & (frame3m.index.normalize() == t.normalize())]
    if sess.empty:
        return "eod", round(float(entry), 2), 0.0, t
    long = direction == "long"
    highs, lows, idx = sess["high"].to_numpy(), sess["low"].to_numpy(), sess.index
    for k in range(len(highs)):
        h, lo = float(highs[k]), float(lows[k])
        if long and lo <= stop:
            return "loss", round(stop, 2), round(stop - entry, 2), idx[k]
        if long and h >= target:
            return "win", round(target, 2), round(target - entry, 2), idx[k]
        if (not long) and h >= stop:
            return "loss", round(stop, 2), round(entry - stop, 2), idx[k]
        if (not long) and lo <= target:
            return "win", round(target, 2), round(entry - target, 2), idx[k]
    close_last = float(sess["close"].iloc[-1])
    pts = (close_last - entry) if long else (entry - close_last)
    return "eod", round(close_last, 2), round(pts, 2), idx[-1]


def list_triggers(
    feats_by_tf: dict[str, pd.DataFrame],
    frames_by_tf: dict[str, pd.DataFrame],
    cfg: MTFDirectionalConfig | None = None,
    size_lots: int = DEFAULT_SIZE_LOTS,
    lot_size: int = LOT_SIZE,
    realistic: bool = False,
    target_driven: bool = False,
    min_stop: float = 0.0,
    atr_mult: float = 0.0,
    atr_period: int = 14,
    skip_open_min: int = 0,
) -> list[dict]:
    """Enumerate EVERY Trade-1 trigger across the full history (all sessions).

    Same flip-detection as ``replay_today`` but without the single-day filter, and
    each trigger's outcome is bounded to its own session via ``simulate_intraday``.
    Returns trigger dicts (engine levels + the true outcome) ordered oldest→newest;
    the index in the list is the ``tid`` the training UI replays.

    ``realistic=True`` (the backtest path) models how you'd actually trade it:
      * **one position at a time** — a fresh trigger is SKIPPED while a prior trade
        is still open, so a single trend that keeps pulling back counts ONCE, not
        N times (kills the cluster-inflation in the raw enumeration);
      * a mark-to-close exit is labelled ``"eod"`` and each trigger carries its
        ``exit_ts``/``exit`` (live + training keep the default, with ``"open"``).
    """
    cfg = cfg or MTFDirectionalConfig()
    if "3min" not in feats_by_tf or "3min" not in frames_by_tf:
        return []
    calls = resolve_direction_mtf(feats_by_tf, cfg)
    if calls.empty:
        return []
    conf, _ = mtf_ema45_confidence(feats_by_tf, calls)
    frame3m = frames_by_tf["3min"]
    bars = frame3m.reindex(calls.index)
    feats = feats_by_tf["3min"].reindex(calls.index)
    c = calls.to_numpy()
    close = bars["close"].to_numpy()
    ts = calls.index
    # Optional ATR-based stop floor (causal Wilder ATR on the 3-min frame): the stop
    # must be at least atr_mult × ATR away, so the floor scales with volatility.
    atr_ser = (_atr(frame3m, atr_period).reindex(calls.index).to_numpy()
               if atr_mult and atr_mult > 0 else None)
    # Optional "skip the opening N minutes" rule: NSE opens 09:15, so a trigger whose
    # time-of-day is before 09:15 + skip_open_min is dropped (opening whipsaw filter).
    open_cutoff = None
    if skip_open_min and skip_open_min > 0:
        from datetime import time as _time
        mins = 9 * 60 + 15 + skip_open_min
        open_cutoff = _time(mins // 60, mins % 60)

    out: list[dict] = []
    busy_until = None        # realistic mode: in a trade until its exit timestamp
    for i in range(len(c)):
        prev = c[i - 1] if i > 0 else "flat"
        if c[i] not in ("long", "short") or c[i] == prev:
            continue
        if realistic and busy_until is not None and ts[i] <= busy_until:
            continue                                  # still in a position — one at a time
        if open_cutoff is not None and ts[i].time() < open_cutoff:
            continue                                  # inside the opening-whipsaw window
        direction, entry = c[i], float(close[i])
        row = feats.iloc[i]
        levels = {k: _f(row.get(k)) for k in
                  ("ema_45", "supertrend", "cpr_pivot", "cpr_tc", "cpr_bc")}
        levels.update(_session_extremes(frame3m, ts[i]))
        eff_min_stop = min_stop
        if atr_ser is not None and pd.notna(atr_ser[i]):
            eff_min_stop = max(eff_min_stop, atr_mult * float(atr_ser[i]))
        stop, target, rr = trade1_levels(direction, entry, levels,
                                          target_driven=target_driven, min_stop=eff_min_stop)
        if stop is None or target is None:
            continue
        if realistic:
            outcome, exit_px, points, exit_ts = _resolve_intraday(
                frame3m, ts[i], direction, entry, stop, target)
            busy_until = exit_ts
        else:
            outcome, exit_px, points = simulate_intraday(
                frame3m, ts[i], direction, entry, stop, target)
            exit_ts = None
        rec = {
            "tid": len(out), "ts": ts[i].isoformat(), "date": str(ts[i].date()),
            "direction": direction, "entry": round(entry, 2),
            "eng_stop": round(stop, 2), "eng_target": round(target, 2), "eng_rr": rr,
            "mtf_confidence": int(conf.iloc[i]),
            "outcome": outcome, "points": round(points, 2),
            "rupees": round(points * lot_size * size_lots, 0),
        }
        if realistic:
            rec["exit_ts"] = exit_ts.isoformat()
            rec["exit"] = exit_px
        out.append(rec)
    return out


def replay_today(
    feats_by_tf: dict[str, pd.DataFrame],
    frames_by_tf: dict[str, pd.DataFrame],
    cfg: MTFDirectionalConfig | None = None,
    size_lots: int = DEFAULT_SIZE_LOTS,
    lot_size: int = LOT_SIZE,
    session_date=None,
) -> dict:
    """Replay one session's Trade-1 triggers and their outcomes.

    ``session_date`` (a ``date`` or ``YYYY-MM-DD`` string) picks which session to replay;
    default ``None`` = the latest session in the frame (back-compat). Lets the cockpit
    browse previous days from the multi-day live pull.
    """
    cfg = cfg or MTFDirectionalConfig()
    empty = {"session": None, "triggers": [], "last": None,
             "summary": {"n": 0, "wins": 0, "losses": 0, "open": 0,
                         "net_points": 0.0, "net_rupees": 0.0, "hit_rate": None}}
    if "3min" not in feats_by_tf or "3min" not in frames_by_tf:
        return empty

    calls = resolve_direction_mtf(feats_by_tf, cfg)
    if calls.empty:
        return empty

    today = calls.index[-1].date() if session_date is None else pd.Timestamp(session_date).date()
    in_day = pd.Index([ts.date() == today for ts in calls.index])
    calls = calls[in_day.values]
    conf, _ = mtf_ema45_confidence(feats_by_tf, calls)
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
        levels.update(_session_extremes(frames_by_tf["3min"], ts[i]))
        stop, target, rr = trade1_levels(direction, entry, levels)
        if stop is None or target is None:
            continue

        outcome, exit_px, points = simulate_trade(
            direction, entry, stop, target, high[i + 1:], low[i + 1:], close[-1])
        # ₹ is sized by THIS trigger's conviction (1-2 lot band), matching the live
        # proposal — not a flat lot count — so the table and the proposal agree.
        row_lots = size_for_confidence(int(conf.iloc[i]))
        triggers.append({
            "ts": ts[i].isoformat(), "direction": direction,
            "entry": round(entry, 2), "stop": round(stop, 2), "target": round(target, 2),
            "rr": rr, "mtf_confidence": int(conf.iloc[i]), "size_lots": row_lots,
            "outcome": outcome, "points": round(points, 2),
            "rupees": round(points * lot_size * row_lots, 0),
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
            "net_points": net_pts,
            "net_rupees": round(sum(t["rupees"] for t in triggers), 0),  # per-row conviction
            "hit_rate": round(wins / (wins + losses), 2) if (wins + losses) else None,
        },
    }
