"""Claude's post-outcome reason-why — the learning post-mortem on a resolved trigger.

After a 3-min breakout-pullback trigger RESOLVES (win/loss), ``explain_outcome``
asks Claude WHY it went the way it did and — graded by PROCESS, not P&L — whether
the trigger itself was a *genuine* setup or a *false* one (e.g. the marginal 5-EMA
graze the trader flagged). The verdict is stored on the decision and distilled back
into the learning memory, so the take/skip read sharpens against the trader's own
genuine/false labels.

Same shape as ``agent.read``: build the constitution system prompt (+ memory), build
a focused user prompt, call an injectable ``completer`` (Anthropic by default), parse
structured JSON ourselves so the module is testable without the SDK.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from agent.prompt import build_system

MODEL = "claude-opus-4-8"

_SCHEMA = {
    "type": "object",
    "properties": {
        "why": {
            "type": "string",
            "description": "Why this trade won/lost — the MECHANISM (what price actually "
            "did versus the breakout-pullback setup and its stop/target), in 1-2 sentences.",
        },
        "trigger_quality": {
            "type": "string",
            "enum": ["genuine", "false"],
            "description": "Graded by PROCESS not P&L: was the breakout-pullback trigger a "
            "GENUINE setup (a clean close below the 5-EMA after a real breakout, holding the "
            "45-EMA) or FALSE/weak (a marginal 5-EMA graze, no real extension, broken trend)? "
            "A lucky win on a false trigger is still 'false'; an unlucky loss on a genuine one "
            "is still 'genuine'.",
        },
        "lesson": {
            "type": "string",
            "description": "One sharp line to remember next time (feeds the learning memory). "
            "Do NOT reinforce a dangerous lucky win or damn a good-process loss.",
        },
    },
    "required": ["why", "trigger_quality", "lesson"],
    "additionalProperties": False,
}


@dataclass
class ReasonWhy:
    why: str
    trigger_quality: str         # "genuine" | "false"
    lesson: str

    @property
    def genuine(self) -> bool:
        return self.trigger_quality == "genuine"


def _g(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def build_reason_user(ctx: dict) -> str:
    """Render the resolved-trigger context into the post-mortem user prompt."""
    out = (ctx.get("outcome") or {})
    read = (ctx.get("chart_read") or {})
    lines = [
        "A 3-min breakout-pullback trigger has RESOLVED. Post-mortem it (process, not P&L).",
        "",
        f"Instrument: {ctx.get('instrument', '?')}   Bar: {ctx.get('ts', '?')}",
        f"Trigger: {ctx.get('direction', '?')}  entry {ctx.get('entry', '?')}  "
        f"stop {ctx.get('stop', '?')}  target {ctx.get('target', '?')}",
        f"Trader action: {ctx.get('action', '?')}"
        + (f"   trader's label: {ctx['trigger_label']}" if ctx.get("trigger_label") else ""),
        f"Outcome: {_g(out, 'status', default='?')}  "
        f"({_g(out, 'points', default='?')} pts, exit {_g(out, 'exit', default='?')})",
    ]
    if read:
        lines += [
            "",
            "As-of chart read:",
            f"  MTF call {read.get('mtf_call', '?')}; daily 45-EMA regime "
            f"{read.get('regime_45_daily', '?')}; 3m Supertrend {read.get('supertrend_3m', '?')}; "
            f"MTF confidence {read.get('mtf_confidence', '?')}/5.",
        ]
        nums = read.get("numbers") or {}
        if nums:
            lines.append(
                f"  RSI {nums.get('rsi_14', '?')}; EMA5 {nums.get('ema_5', '?')}; "
                f"EMA45 {nums.get('ema_45', '?')}; Supertrend {nums.get('supertrend', '?')}.")
    lines += [
        "",
        "Return: why it won/lost (the mechanism), whether the TRIGGER was genuine or false "
        "(by process), and one lesson to remember.",
    ]
    return "\n".join(lines)


def _default_completer(system: str, user: str) -> ReasonWhy:
    """Call the Anthropic API (reads ANTHROPIC_API_KEY from the env)."""
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "Reason-why needs the Anthropic SDK: pip install anthropic, and set "
            "ANTHROPIC_API_KEY in the environment."
        ) from exc

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
    )
    text = next(b.text for b in resp.content if b.type == "text")
    return ReasonWhy(**json.loads(text))


def explain_outcome(ctx: dict, memory_text: str = "", completer=None) -> ReasonWhy:
    """Post-mortem a resolved trigger → ``ReasonWhy``.

    ``ctx`` carries the trigger (direction/entry/stop/target), the trader's
    action + optional genuine/false label, the resolved ``outcome`` dict, and the
    as-of ``chart_read``. ``completer(system, user) -> ReasonWhy`` defaults to the
    live Anthropic call; pass a stub in tests.
    """
    system = build_system(memory_text)
    user = build_reason_user(ctx)
    completer = completer or _default_completer
    return completer(system, user)
