"""Agent layer — Claude as the reasoning + sparring engine on top of the
deterministic chart engine.

The chart engine (indicators -> voters -> resolver -> discipline gate) produces
auditable numbers; Claude reads them against the journal, challenges the trader's
bias, and recommends ENTER / STAND_DOWN — the sparring contract from the journal.
A learning loop (``agent.memory``) feeds the logged decision history back into
Claude's system prompt so its challenges sharpen on the trader's own edge.

Live calls go to the Anthropic API (model ``claude-opus-4-8``) and run on the
user's machine; the API call is injectable so the logic is testable offline.
"""
