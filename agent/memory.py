"""The learning loop — distill the logged decision history into a compact memory
block that gets injected into Claude's system prompt.

Every proposal + the human's approve/reject is appended to ``results/decisions.jsonl``
by ``journal.log_decision``. Distilling that history back into context is the
practical "self-improving" loop: over time Claude sees its own track record and
the trader's recurring patterns, so its challenges sharpen on the real edge.

This stays deterministic and rule-based (no LLM call) so it is fast and testable;
a Claude-written summary can replace ``distill_memory`` later if richer synthesis
is wanted.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from journal.log import DEFAULT_LOG


def load_decisions(path: str | Path = DEFAULT_LOG) -> list[dict]:
    """Read the append-only decision log (missing file -> empty history)."""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def distill_memory(decisions: list[dict], max_recent: int = 15) -> str:
    """Summarise the decision history into a compact system-prompt memory block."""
    if not decisions:
        return "No prior decisions logged yet — this is an early session."

    n = len(decisions)
    approved = sum(1 for d in decisions if d.get("decision") == "approved")
    rejected = n - approved
    rec = Counter(
        (d.get("proposal", {}) or {}).get("recommendation") for d in decisions
    )
    # Most common stand-down reasons (first reason line of each STAND_DOWN proposal)
    stand_reasons = Counter()
    for d in decisions:
        prop = d.get("proposal", {}) or {}
        if prop.get("recommendation") == "stand_down" and prop.get("reasons"):
            stand_reasons[prop["reasons"][0][:80]] += 1

    lines = [
        f"Decisions logged: {n} (approved {approved}, rejected {rejected}).",
        f"Engine recommendations: ENTER {rec.get('enter', 0)}, "
        f"STAND_DOWN {rec.get('stand_down', 0)}.",
    ]
    if stand_reasons:
        top = "; ".join(f"{r} (x{c})" for r, c in stand_reasons.most_common(3))
        lines.append(f"Recurring stand-down reasons: {top}.")

    # Process x outcome 2x2 (settled trades) — grade by PROCESS, not P&L.
    cells = Counter(d.get("matrix") for d in decisions if d.get("matrix"))
    if any(cells.get(k) for k in ("deserved", "accept", "dangerous", "correct")):
        lines.append(
            "Track record (process×outcome): "
            f"deserved {cells.get('deserved', 0)} (good process, won), "
            f"accept {cells.get('accept', 0)} (good process, lost — variance), "
            f"dangerous {cells.get('dangerous', 0)} (BAD process, won — luck), "
            f"correct {cells.get('correct', 0)} (bad process, lost)."
        )
    if cells.get("dangerous"):
        lines.append(
            f"⚠️ {cells['dangerous']} 'dangerous' trade(s) made money on BAD process. "
            "Do NOT let these reinforce — they are the Session-002 trap. Grade by "
            "process: challenge oversize / mid-box / override-the-gate entries even "
            "when the last one paid."
        )

    recent = decisions[-max_recent:]
    lines.append("Recent decisions (oldest→newest):")
    for d in recent:
        prop = d.get("proposal", {}) or {}
        lines.append(
            f"  - {prop.get('ts', '?')} {prop.get('instrument', '?')} "
            f"{prop.get('direction', '?')} → engine {prop.get('recommendation', '?')}, "
            f"trader {d.get('decision', '?')}"
        )
    return "\n".join(lines)


def distill_context(records: list[dict], max_recent: int = 8) -> str:
    """Distil the full-context store into the reasoning the agent should learn from.

    Where ``distill_memory`` summarises *that* a trade was taken, this surfaces *why*:
    Claude's verdict + key risk on each recent decision, the trader's final call, and
    how it actually settled (with the 2x2 cell). Feeds richer self-improvement — the
    agent sees its own past reads against outcomes, not just tallies.
    """
    rich = [r for r in records if r.get("claude_read") or r.get("chat")]
    if not rich:
        return ""
    lines = ["", "Past reasoning vs. outcomes (learn from these — your own reads graded "
             "by PROCESS, not P&L; never reinforce a 'dangerous' lucky win):"]
    for r in rich[-max_recent:]:
        prop = r.get("proposal") or {}
        read = r.get("claude_read") or {}
        outcome = r.get("outcome") or {}
        verdict = read.get("recommendation") or "?"
        risk = (read.get("key_risk") or "").strip().replace("\n", " ")
        if len(risk) > 140:
            risk = risk[:137] + "…"
        settled = outcome.get("status")
        tail = f"settled {settled}" if settled and settled != "open" else "open/unsettled"
        cell = r.get("matrix")
        tag = "TRAINING replay" if r.get("kind") == "training" else "Live"
        reason = (prop.get("reason") or "").strip().replace("\n", " ")
        if len(reason) > 160:
            reason = reason[:157] + "…"
        lines.append(
            f"  - [{tag}] {r.get('ts', '?')} {prop.get('direction', '?')} · Claude {verdict} "
            f"(conf {read.get('confidence', '?')}/5), trader {r.get('decision', '?')} "
            f"→ {tail}{f' [{cell}]' if cell else ''}"
            + (f" · risk: {risk}" if risk else "")
            + (f" · trader's reason: {reason}" if reason else ""))
    return "\n".join(lines)
