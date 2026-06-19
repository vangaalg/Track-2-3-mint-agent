"""The TradeProposal contract — the single object that flows
snapshot → analysis → dashboard → execution → journal.

A proposal is *propose-only*: it never executes itself. The dashboard shows it,
the human approves or rejects, and only an APPROVED proposal is handed to
``execution.place``. The ``recommendation`` is the agent's sparring verdict
(ENTER / STAND_DOWN), not a command.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum


class Recommendation(str, Enum):
    ENTER = "enter"
    STAND_DOWN = "stand_down"


# The journal's pre-trade discipline lines. A proposal that leaves any of these
# unfilled is a STAND_DOWN by construction (the "no answer = no trade" rule).
SIX_LINES = ("edge", "stop", "size", "invalidation", "target", "time_container")


@dataclass
class TradeProposal:
    """One vetted trade idea for one instrument and one bucket (Trade 1/2/3)."""

    instrument: str
    trade_type: str                      # "trade1" | "trade2" | "trade3"
    ts: str                              # ISO timestamp of the snapshot bar
    direction: str                       # "long" | "short" | "flat"

    # Machine B — levels & vehicle (None when STAND_DOWN)
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    size_lots: int | None = None
    vehicle: str | None = None           # e.g. "NIFTY 23700 CE (deep-ITM)"
    rupee_risk: float | None = None      # approx, at full size
    rr_ratio: float | None = None        # reward : risk

    # MTF 45-EMA conviction (0..5): higher TFs with price on the signal's side.
    mtf_confidence: int = 0

    # The discipline gate
    recommendation: Recommendation = Recommendation.STAND_DOWN
    checklist: dict[str, str] = field(default_factory=dict)   # the six lines
    reasons: list[str] = field(default_factory=list)          # human-readable why

    # Context carried for the dashboard / journal (compact)
    spot: float | None = None
    context: dict = field(default_factory=dict)               # read + oi + macro summary

    def as_dict(self) -> dict:
        d = asdict(self)
        d["recommendation"] = self.recommendation.value
        return d

    @property
    def is_enter(self) -> bool:
        return self.recommendation is Recommendation.ENTER
