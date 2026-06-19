"""The discipline gate — the journal's pre-trade check, encoded.

"No answer to all the lines = NO TRADE." This is the sparring layer: it does not
generate signals, it *blocks* the ones that fail the contract. A proposal whose
six lines aren't all filled — or whose read is flat/conflicted — is forced to
STAND_DOWN (a logged no-trade, which the journal counts as a win).
"""

from __future__ import annotations

from analysis.proposal import Recommendation, SIX_LINES

# Normal size band (lots) from the journal's size-discipline rule. Outside this
# band the check fails unless explicitly flagged a high-quality wall/support edge.
NORMAL_SIZE_LO, NORMAL_SIZE_HI = 65, 130


def evaluate(checklist: dict[str, str], direction: str, size_lots: int | None) -> tuple[Recommendation, list[str]]:
    """Return (recommendation, reasons) for a filled-in checklist.

    A trade is ENTER only if: the read is directional (not flat), every one of the
    six lines is filled, and size sits in the normal band. Any failure → STAND_DOWN
    with the reason, plainly.
    """
    reasons: list[str] = []

    if direction not in ("long", "short"):
        return Recommendation.STAND_DOWN, ["No edge: read is flat/conflicted — STAND DOWN."]

    blanks = [line for line in SIX_LINES if not checklist.get(line)]
    if blanks:
        return (
            Recommendation.STAND_DOWN,
            [f"Six-line check incomplete: missing {', '.join(blanks)} — STAND DOWN."],
        )

    if size_lots is None or not (NORMAL_SIZE_LO <= size_lots <= NORMAL_SIZE_HI):
        return (
            Recommendation.STAND_DOWN,
            [
                f"Size {size_lots} outside the normal {NORMAL_SIZE_LO}-{NORMAL_SIZE_HI} "
                "band — STAND DOWN ('I strongly believe' is a warning light)."
            ],
        )

    reasons.append("Six-line check complete; size within normal band; read is directional.")
    return Recommendation.ENTER, reasons
