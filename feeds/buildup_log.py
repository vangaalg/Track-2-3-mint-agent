"""CLEAR-lean transition log — WHEN an instrument entered clear bull / clear bear and
HOW LONG it has held that state.

The OI-buildup lean (``feeds.oi_buildup.buildup_signal`` → ``buildup_bias`` +
``buildup_score``) is already persisted per recording cycle in ``feeds.oi_summary_store``
(the ``oi_summary`` series). This module is a PURE derivation over that stored series: it
grades every row's lean into a 5-step state (clear_bull · bull · neutral · bear ·
clear_bear) and collapses contiguous runs of a CLEAR state into *episodes* — start time,
the state it transitioned FROM (e.g. bull → clear_bull), how long it lasted, and whether
it is still holding right now.

Example (the trader's ask): NIFTY crosses bull → clear_bull at 09:40 and is still clear
at 09:50 ⇒ one holding episode, ``from_state="bull"``, ``duration_min=10``. It reads the
duration off the recorded timestamps, so a holding episode grows as new cycles land
(recorder ~15m for indices, and the live cockpit writes a summary row on each fresh OI
bucket, so intraday granularity is finer while the cockpit is open).

No new write path — it derives from what the recorder/cockpit already store, so it works
retroactively over the whole recorded day and needs nothing extra to accrue.
"""

from __future__ import annotations

import pandas as pd

STRONG = 0.4      # |score| for a CLEAR lean (mirrors analysis.trade1.BUILDUP_STRONG + the UI)
MILD = 0.15       # |score| for a mild lean (mirrors feeds.oi_buildup.buildup_signal thresholds)

CLEAR_BULL = "clear_bull"
CLEAR_BEAR = "clear_bear"
_CLEAR = (CLEAR_BULL, CLEAR_BEAR)


def _isnan(x) -> bool:
    try:
        return x is None or (isinstance(x, float) and x != x)
    except Exception:
        return True


def grade_state(score, strong: float = STRONG, mild: float = MILD) -> str:
    """The 5-step lean state for one score (same thresholds as the aggregate + the CLEAR bar).

    ``clear_bull`` >= strong · ``bull`` >= mild · ``neutral`` · ``bear`` <= -mild ·
    ``clear_bear`` <= -strong. NaN / None → ``neutral`` (honest, not a guess)."""
    if _isnan(score):
        return "neutral"
    s = float(score)
    if s >= strong:
        return CLEAR_BULL
    if s >= mild:
        return "bull"
    if s <= -strong:
        return CLEAR_BEAR
    if s <= -mild:
        return "bear"
    return "neutral"


def _minutes(start_ts: str, end_ts: str) -> float:
    """Whole-minute gap between two IST-iso timestamps (0 for a single-sample episode)."""
    try:
        d = (pd.Timestamp(end_ts) - pd.Timestamp(start_ts)).total_seconds() / 60.0
        return round(max(d, 0.0), 1)
    except Exception:
        return 0.0


def clear_episodes(df: pd.DataFrame | None, symbol: str = "",
                   strong: float = STRONG, mild: float = MILD) -> list[dict]:
    """Contiguous CLEAR-state episodes from a ts-indexed ``oi_summary`` frame (chronological).

    Each episode dict:
      symbol, state (clear_bull/clear_bear), bias (bullish/bearish),
      start, end (first / last recorded ts still in the state),
      duration_min (end - start), holding (True if the run reaches the last stored row),
      from_state (the graded state right before it entered — e.g. "bull"),
      to_state (what broke it, or None while holding),
      samples (rows in the run), peak_score, spot_at_start.
    """
    if df is None or getattr(df, "empty", True) or "buildup_score" not in df.columns:
        return []
    d = df.sort_index()
    ts_list = [str(t) for t in d.index]
    scores = list(pd.to_numeric(d["buildup_score"], errors="coerce"))
    spots = (list(pd.to_numeric(d["spot"], errors="coerce"))
             if "spot" in d.columns else [None] * len(d))
    states = [grade_state(s, strong, mild) for s in scores]

    episodes: list[dict] = []
    i, n = 0, len(states)
    while i < n:
        st = states[i]
        if st not in _CLEAR:
            i += 1
            continue
        day = ts_list[i][:10]
        j = i
        peak = scores[i]
        # Extend the run only while the SAME clear state AND the SAME session (date). A lean
        # does not "hold" across an overnight market close, so a date change ends the episode —
        # otherwise a still-clear open would merge into yesterday and read as a 20-hour hold.
        while j + 1 < n and states[j + 1] == st and ts_list[j + 1][:10] == day:
            j += 1
            if not _isnan(scores[j]) and (_isnan(peak) or abs(scores[j]) > abs(peak)):
                peak = scores[j]
        holding = j == n - 1
        next_same_day = (j + 1 < n) and (ts_list[j + 1][:10] == day)
        prev_same_day = (i > 0) and (ts_list[i - 1][:10] == day)
        start_ts, end_ts = ts_list[i], ts_list[j]
        episodes.append({
            "symbol": symbol,
            "state": st,
            "bias": "bullish" if st == CLEAR_BULL else "bearish",
            "date": day,
            "start": start_ts,
            "end": end_ts,
            "duration_min": _minutes(start_ts, end_ts),   # recorded span; the live "now-turn" is on the client
            "holding": holding,
            "from_state": states[i - 1] if prev_same_day else None,
            "to_state": states[j + 1] if next_same_day else None,
            "samples": j - i + 1,
            "peak_score": None if _isnan(peak) else round(float(peak), 3),
            "spot_at_start": None if _isnan(spots[i]) else round(float(spots[i]), 1),
        })
        i = j + 1
    return episodes


def collect(symbols, load_summary_fn, strong: float = STRONG,
            mild: float = MILD) -> list[dict]:
    """CLEAR episodes across many instruments (``load_summary_fn(symbol)`` → summary frame or
    None), sorted holding-first then newest-start — so the still-live leans surface at the top.
    A failing/absent instrument contributes nothing (never raises)."""
    out: list[dict] = []
    for s in symbols:
        try:
            df = load_summary_fn(s)
        except Exception:
            df = None
        out.extend(clear_episodes(df, symbol=s, strong=strong, mild=mild))
    out.sort(key=lambda e: (e["holding"], e["start"]), reverse=True)
    return out
