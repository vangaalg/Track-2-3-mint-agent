"""Assemble Claude's system prompt (the sparring constitution + learning memory)
and the per-call user prompt (the structured snapshot + the engine's proposal).
"""

from __future__ import annotations

from pathlib import Path

_CONSTITUTION = Path(__file__).parent / "SPARRING_PROMPT.md"


def build_system(memory_text: str = "") -> str:
    """System prompt = the frozen sparring constitution + the learning memory.

    The constitution is byte-stable (good for prompt caching); the volatile memory
    block goes last.
    """
    constitution = _CONSTITUTION.read_text()
    if not memory_text:
        return constitution
    return (
        constitution
        + "\n\n## Your track record so far (learning memory)\n"
        + "Use this to sharpen your challenge; call out repeated patterns.\n\n"
        + memory_text
    )


def _fmt(x) -> str:
    return "—" if x is None else str(x)


def build_user(snapshot, proposal) -> str:
    """Render the snapshot + deterministic proposal into the user message."""
    read = getattr(snapshot, "chart_read", {}) or {}
    lv = read.get("levels", {})
    oi = getattr(snapshot, "oi", None)
    macro = getattr(snapshot, "macro", None)

    parts = [
        f"INSTRUMENT: {snapshot.instrument}   SPOT: {snapshot.spot}   "
        f"BAR: {getattr(snapshot, 'ts', '?')}",
        "",
        "CHART READ (deterministic engine):",
        f"  MTF call: {read.get('mtf_call')}",
        f"  Daily 45-EMA regime: {read.get('regime_45_daily')}  "
        f"(+1 up / -1 down)",
        f"  3m Supertrend dir: {read.get('supertrend_3m')}   "
        f"3m EMA5 trigger: {read.get('ema5_trigger_3m')}",
        f"  Levels — 45-EMA {_fmt(lv.get('ema_45'))}, Supertrend "
        f"{_fmt(lv.get('supertrend'))}, CPR pivot {_fmt(lv.get('cpr_pivot'))} "
        f"(TC {_fmt(lv.get('cpr_tc'))} / BC {_fmt(lv.get('cpr_bc'))})",
    ]

    if oi:
        parts.append(
            f"  OI — PCR {_fmt(oi.get('pcr'))}, call wall "
            f"{_fmt((oi.get('call_wall') or {}).get('strike'))}, put shelf "
            f"{_fmt((oi.get('put_shelf') or {}).get('strike'))}, max-pain "
            f"{_fmt(oi.get('max_pain'))}"
        )
    else:
        parts.append("  OI — unavailable")
    parts.append(f"  Macro — {_fmt(macro)}")

    rec = getattr(proposal, "recommendation", None)
    rec = rec.value if hasattr(rec, "value") else rec
    parts += [
        "",
        "ENGINE PROPOSAL (Trade 1):",
        f"  Recommendation: {rec}",
        f"  Direction: {proposal.direction}   Size: {_fmt(proposal.size_lots)} lots",
        f"  Entry {_fmt(proposal.entry)} / Stop {_fmt(proposal.stop)} / "
        f"Target {_fmt(proposal.target)}   R:R {_fmt(proposal.rr_ratio)}",
        f"  Vehicle: {_fmt(proposal.vehicle)}   Approx ₹risk: {_fmt(proposal.rupee_risk)}",
        "  Six-line check: "
        + ", ".join(f"{k}={v}" for k, v in (proposal.checklist or {}).items()),
        "  Engine reasons: " + " | ".join(proposal.reasons or []),
        "",
        "Spar with this. Give your thesis, challenge the trader's most likely trap "
        "for THIS setup, say whether you agree with the engine, and recommend "
        "ENTER or STAND_DOWN.",
    ]
    return "\n".join(parts)
