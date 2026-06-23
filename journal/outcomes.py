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
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pandas as pd

from analysis.triggers import _resolve_intraday
from journal.log import DEFAULT_LOG
from analysis.trade1 import LOT_SIZE

_IST = timezone(timedelta(hours=5, minutes=30))
_SESSION_CLOSE = time(15, 30)


def _session_live(ts) -> bool:
    """True while the trade's OWN session is still in progress, so an unresolved trade
    stays ``open`` instead of being marked-to-close. False once 15:30 IST has passed for
    that date (or the date is already in the past) — then ``settle`` auto marks-to-close."""
    d = pd.Timestamp(ts).date()
    now = datetime.now(_IST)
    if d < now.date():
        return False
    if d > now.date():            # future-dated (shouldn't happen) — treat as live
        return True
    return now.time() < _SESSION_CLOSE

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


_TRAINING_MATRIX = {
    ("take", "win"): "deserved",   ("take", "loss"): "accept",
    ("skip", "win"): "missed",     ("skip", "loss"): "avoided",
}


def grade_training(action: str, outcome_status: str | None) -> str:
    """2x2 cell for a TRAINING replay (trader take/skip vs the known outcome).

    take+win -> deserved (good entry), take+loss -> accept (valid signal, variance),
    skip+would-win -> missed (passed a winner), skip+would-loss -> avoided (good
    discipline). Unresolved -> open.
    """
    if outcome_status in (None, "open"):
        return "open"
    return _TRAINING_MATRIX.get((action, outcome_status), "pending")


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
        # Session-bounded resolution (no cross-day leak) + auto mark-to-close at 15:30:
        # a trade that hits neither stop nor target is exited "eod" at its own session's
        # close — kept "open" only while that session is still live.
        outcome, exit_px, points, exit_ts = _resolve_intraday(
            bars, ts, direction, entry, stop, target)
        if outcome == "eod" and _session_live(ts):
            r["matrix"] = _matrix(r["process_grade"], None)   # still open today — wait
            continue
        status = outcome if outcome in ("win", "loss") else ("win" if points >= 0 else "loss")
        size = p.get("size_lots") or 75
        r["outcome"] = {"status": status, "exit": exit_px, "points": points,
                        "rupees": round(points * lot_size * size, 0),
                        "eod": outcome == "eod",
                        "exit_ts": exit_ts.isoformat() if exit_ts is not None else None,
                        "settled_at": datetime.now(timezone.utc).isoformat()}
        r["matrix"] = _matrix(r["process_grade"], status)
        changed = True
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


def settle_store(frames_by_tf: dict[str, pd.DataFrame] | None = None,
                 path: str | Path | None = None) -> list[dict]:
    """Settle the SQLite full-context store with the same 2x2 grading as the log.

    Reads the rich decision rows, grades + (where forward bars exist) resolves each,
    and writes the process grade / 2x2 cell / outcome back onto the row. Returns the
    settled records (so the caller can summarise the track record from the store).
    """
    from journal import store as _store
    p = path or _store.DB_PATH
    records = _store.load_records(p, kind="live")   # training rows grade at save time
    if not records:
        return []
    settled, _ = settle(records, frames_by_tf or {})
    for r in settled:
        _store.update_outcome(
            r["id"], r.get("outcome") or {}, r.get("process_grade"), r.get("matrix"),
            path=p)
    return settled


def manual_exit_outcome(proposal: dict, exit_px: float, exit_ts: str | None = None,
                        lot_size: int = LOT_SIZE) -> dict:
    """Outcome for a trader's MANUAL exit at ``exit_px`` (records realized P&L now instead of
    waiting for the auto-settle at stop/target/EOD). Same shape as ``settle`` writes, so
    ``store.update_outcome`` persists it unchanged; status is win/loss by P&L sign, so it slots
    straight into the 2x2 via ``_matrix``."""
    direction = proposal.get("direction")
    entry = proposal.get("entry")
    size = proposal.get("size_lots") or 75
    points = round((exit_px - entry) if direction == "long" else (entry - exit_px), 2)
    status = "win" if points >= 0 else "loss"
    return {"status": status, "exit": round(exit_px, 2), "points": points,
            "rupees": round(points * lot_size * size, 0), "exit_ts": exit_ts,
            "manual": True, "settled_at": datetime.now(timezone.utc).isoformat()}


def matrix_summary(decisions: list[dict]) -> dict:
    """Counts per 2x2 cell + net points/₹ of settled trades."""
    from collections import Counter
    cells = Counter(d.get("matrix") for d in decisions if d.get("matrix"))
    settled = [d["outcome"] for d in decisions if d.get("outcome") and d["outcome"].get("status") != "open"]
    net_pts = round(sum(o.get("points", 0) for o in settled), 2)
    net_rs = round(sum(o.get("rupees", 0) for o in settled), 0)
    return {"cells": dict(cells), "n_settled": len(settled),
            "net_points": net_pts, "net_rupees": net_rs}
