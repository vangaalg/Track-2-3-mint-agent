"""Append proposal + decision records to a JSONL training log."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from analysis.proposal import TradeProposal

DEFAULT_LOG = "results/decisions.jsonl"


def log_decision(
    proposal: TradeProposal,
    decision: str,                 # "approved" | "rejected"
    execution: dict | None = None,
    path: str | Path = DEFAULT_LOG,
) -> dict:
    """Append one record and return it. ``decision`` is the human's call."""
    record = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "decision": decision,
        "proposal": proposal.as_dict(),
        "execution": execution,
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(json.dumps(record) + "\n")
    return record
