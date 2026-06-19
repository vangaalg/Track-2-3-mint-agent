"""Close the learning loop — grade logged decisions on the journal's 2x2.

Each decision is graded on BOTH axes, exactly as the journal demands (so a *lucky
bad-process win* never trains the agent to repeat it — Session 002):

  process  : good (took an engine-ENTER that passed the six-line gate) /
             override (approved against a STAND_DOWN) / no_trade (rejected)
  outcome  : win / loss / open (settled by simulating entry→stop/target forward)

  matrix cell = process × outcome:
    good + win      -> deserved   ✅   (repeat exactly)
    good + loss     -> accept     😐   (variance — change nothing)
    override + win  -> dangerous  ⚠️   (the trap — do NOT reinforce)
    override + loss -> correct    🔴   (honest lesson)
    rejected        -> no_trade   ✅   (no-trade is a win)

``settle`` fills the outcome of any approved/rejected ENTER once enough bars exist;
``settle_log`` persists it back to ``results/decisions.jsonl``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from analysis.triggers import simulate_trade
from journal.log import DEFAULT_LOG
from analysis.trade1 import LOT_SIZE

_MATRIX = {
    ("good", "win"): "deserved", ("good", "loss"): "accept",
    ("override", "win"): "dangerous", ("override", "loss"): "correct",
}


def grade_process(record: dict) -> str:
    """good / override / no_trade from the decision + the engine recommendation."""
    decision = record.get("decision")
    rec = (record.get("proposal") or {}).get("recommendation")
    if decision == "rejected":
        return "no_trade"
    if decision == "approved":
        return "good" if rec == "enter" else "override"
    return "unknown"


def _matrix(process: str, outcome: str | None) -> str:
    if process == "no_trade":
        return "no_trade"
    if outcome in (None, "open"):
        return "open" if outcome == "open" else "pending"
    return _MATRIX.get((process, outcome), "pending")


def settle(decisions: list[dict], frames_by_tf: dict[str, pd.DataFrame],
           lot_size: int = LOT_SIZE) -> tuple[list[dict], bool]:
    """Grade + (where data exists) resolve the outcome of each decision in place.

    Returns ``(decisions, changed)``. ``changed`` is True if any record gained a
    new settled outcome (so the caller can persist). Decisions without forward bars
    yet stay pending.
    """
    bars = frames_by_tf.get("3min") if frames_by_tf else None
    changed = False
    for r in decisions:
        r["process_grade"] = grade_process(r)
        if r.get("outcome"):                       # already settled
            r["matrix"] = _matrix(r["process_grade"], r["outcome"].get("status"))
            continue
        p = r.get("proposal") or {}
        entry, stop, target = p.get("entry"), p.get("stop"), p.get("target")
        direction, ts = p.get("direction"), p.get("ts")
        if None in (entry, stop, target) or direction not in ("long", "short") or bars is None:
            r["matrix"] = _matrix(r["process_grade"], None)
            continue
        fwd = bars[bars.index > pd.Timestamp(ts)]
        if fwd.empty:                              # not enough data yet
            r["matrix"] = _matrix(r["process_grade"], None)
            continue
        outcome, exit_px, points = simulate_trade(
            direction, entry, stop, target,
            fwd["high"].to_numpy(), fwd["low"].to_numpy(), fwd["close"].iloc[-1])
        size = p.get("size_lots") or 75
        r["outcome"] = {"status": outcome, "exit": exit_px, "points": points,
                        "rupees": round(points * lot_size * size, 0),
                        "settled_at": datetime.now(timezone.utc).isoformat()}
        r["matrix"] = _matrix(r["process_grade"], outcome)
        changed = outcome != "open" or changed     # only "real" settlements persist
    return decisions, changed


def settle_log(path: str | Path = DEFAULT_LOG,
               frames_by_tf: dict[str, pd.DataFrame] | None = None) -> list[dict]:
    """Read the decision log, settle it, persist if anything resolved, return it."""
    p = Path(path)
    if not p.exists():
        return []
    decisions = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    decisions, changed = settle(decisions, frames_by_tf or {})
    if changed:
        p.write_text("\n".join(json.dumps(d) for d in decisions) + "\n")
    return decisions


def matrix_summary(decisions: list[dict]) -> dict:
    """Counts per 2x2 cell + net points/₹ of settled trades."""
    from collections import Counter
    cells = Counter(d.get("matrix") for d in decisions if d.get("matrix"))
    settled = [d["outcome"] for d in decisions if d.get("outcome") and d["outcome"].get("status") != "open"]
    net_pts = round(sum(o.get("points", 0) for o in settled), 2)
    net_rs = round(sum(o.get("rupees", 0) for o in settled), 0)
    return {"cells": dict(cells), "n_settled": len(settled),
            "net_points": net_pts, "net_rupees": net_rs}
