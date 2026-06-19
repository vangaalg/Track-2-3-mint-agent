"""Interactive sparring chat — the back-and-forth on top of the one-shot read.

``claude_read`` gives the opening verdict; this lets the trader argue back and
Claude push on his invalidation, holding the journal's discipline (only the stated
invalidation is an exit; no-trade is a win; size is the tell). The trade context
(snapshot + proposal) is pinned in the system prompt so every turn stays grounded.

The Anthropic call is injectable, so the conversation logic is testable offline.
"""

from __future__ import annotations

from agent.prompt import build_system, build_user
from agent.read import MODEL


def _setup_system(snapshot, proposal, memory_text: str) -> str:
    """Constitution + learning memory + the current trade context (pinned)."""
    return (
        build_system(memory_text)
        + "\n\n## Current setup (what you are sparring over)\n"
        + build_user(snapshot, proposal)
    )


def _default_completer(system: str, history: list[dict]) -> str:
    """Call the Anthropic API for one chat turn (plain text)."""
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "Sparring chat needs the Anthropic SDK: pip install anthropic, and set "
            "ANTHROPIC_API_KEY in the environment."
        ) from exc

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=system,
        messages=history,
    )
    return "".join(b.text for b in resp.content if b.type == "text")


def spar_turn(
    history: list[dict],
    snapshot,
    proposal,
    memory_text: str = "",
    completer=None,
) -> str:
    """Return Claude's next reply given the conversation ``history``.

    ``history`` is the running list of ``{"role": "user"|"assistant", "content": str}``
    (must end with the trader's latest message). ``completer(system, history) -> str``
    defaults to the live Anthropic call; pass a stub in tests.
    """
    system = _setup_system(snapshot, proposal, memory_text)
    completer = completer or _default_completer
    return completer(system, history)
