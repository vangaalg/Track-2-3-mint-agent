"""Execution layer — propose-only Breeze order adapter.

Hard rule (CONTEXT Phase 4 / SEBI non-algo): nothing here auto-fires. Only an
APPROVED ``TradeProposal`` reaches ``place``; ``place`` is **dry-run by default**
and only contacts the broker when explicitly told to AND an env gate is set. The
Breeze key is assumed Trade+View with **Withdraw disabled**.
"""
