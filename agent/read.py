"""Claude's sparring read — the reasoning layer over the deterministic proposal.

``claude_read`` builds the system + user prompts, calls a *completer* (the
Anthropic API by default, injectable for tests), and returns a structured
``ClaudeRead``. The live call uses the official ``anthropic`` SDK with model
``claude-opus-4-8`` and structured outputs (``output_config.format``) so the
verdict is always parseable. Parsing the JSON ourselves keeps this module free of
a hard pydantic dependency, so the logic is testable without the SDK installed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from agent.prompt import build_system, build_user

MODEL = "claude-opus-4-8"

# JSON schema for the structured sparring verdict (output_config.format).
_SCHEMA = {
    "type": "object",
    "properties": {
        "agrees_with_engine": {"type": "boolean"},
        "chart_analysis": {
            "type": "string",
            "description": "What the chart stack says (45-EMA regime, Supertrend, "
            "CPR, EMA5 trigger, momentum) and the direction it implies.",
        },
        "oi_analysis": {
            "type": "string",
            "description": "What the option chain says — PCR, call wall / put shelf, "
            "max-pain, where writers are pinning. Say 'OI unavailable — chart-only "
            "read' if there is no chain data.",
        },
        "where_moving": {
            "type": "string",
            "description": "Synthesis: the most likely path for price from here.",
        },
        "right_trade": {
            "type": "string",
            "description": "The one right trade reading chart + OI together (vehicle/"
            "direction/level), or 'No trade' if there is no edge.",
        },
        "challenge": {
            "type": "string",
            "description": "The specific journal trap the trader is most at risk of here.",
        },
        "recommendation": {"type": "string", "enum": ["enter", "stand_down"]},
        "confidence": {"type": "integer", "description": "1 (low) to 5 (high)."},
        "key_risk": {"type": "string", "description": "The one thing that breaks this trade."},
    },
    "required": [
        "agrees_with_engine", "chart_analysis", "oi_analysis", "where_moving",
        "right_trade", "challenge", "recommendation", "confidence", "key_risk",
    ],
    "additionalProperties": False,
}


@dataclass
class ClaudeRead:
    agrees_with_engine: bool
    chart_analysis: str
    oi_analysis: str
    where_moving: str
    right_trade: str
    challenge: str
    recommendation: str          # "enter" | "stand_down"
    confidence: int
    key_risk: str

    @property
    def enter(self) -> bool:
        return self.recommendation == "enter"


def _default_completer(system: str, user: str) -> ClaudeRead:
    """Call the Anthropic API (reads ANTHROPIC_API_KEY from the env)."""
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "Claude read needs the Anthropic SDK: pip install anthropic, and set "
            "ANTHROPIC_API_KEY in the environment."
        ) from exc

    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY from env
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
    )
    text = next(b.text for b in resp.content if b.type == "text")
    return ClaudeRead(**json.loads(text))


def claude_read(snapshot, proposal, memory_text: str = "", completer=None) -> ClaudeRead:
    """Produce Claude's sparring read for a snapshot + the engine's proposal.

    ``completer(system, user) -> ClaudeRead`` defaults to the live Anthropic call;
    pass a stub in tests. ``memory_text`` is the distilled decision history
    (see ``agent.memory.distill_memory``) — the learning loop.
    """
    system = build_system(memory_text)
    user = build_user(snapshot, proposal)
    completer = completer or _default_completer
    return completer(system, user)
