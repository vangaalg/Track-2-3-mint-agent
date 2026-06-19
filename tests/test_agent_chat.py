"""Sparring chat — multi-turn conversation logic, offline (stub completer)."""

from __future__ import annotations

from types import SimpleNamespace

from analysis.proposal import TradeProposal, Recommendation
from agent.chat import spar_turn


def _snapshot():
    return SimpleNamespace(
        instrument="NIFTY", ts="2026-06-19T15:00:00+05:30", spot=24042.0,
        chart_read={"mtf_call": "flat", "regime_45_daily": 0, "supertrend_3m": 1,
                    "ema5_trigger_3m": 1, "levels": {"ema_45": 23972.0,
                    "supertrend": 23981.0, "cpr_pivot": 24135.0, "cpr_tc": 24157.0,
                    "cpr_bc": 24113.0}},
        oi=None, macro=None, notes=[],
    )


def _proposal():
    return TradeProposal(
        instrument="NIFTY", trade_type="trade1", ts="2026-06-19T15:00:00+05:30",
        direction="flat", recommendation=Recommendation.STAND_DOWN,
        reasons=["No edge: read is flat/conflicted — STAND DOWN."],
        checklist={},
    )


def test_spar_turn_uses_history_and_context():
    captured = {}

    def fake(system, history):
        captured["system"] = system
        captured["history"] = history
        return "No. The 3m uptick is noise three minutes from close — STAND DOWN."

    history = [{"role": "user", "content": "But the 3m is ticking up, I want a quick scalp."}]
    reply = spar_turn(history, _snapshot(), _proposal(), memory_text="mem!", completer=fake)

    assert "STAND DOWN" in reply
    # The constitution, learning memory, and current setup are all in the system.
    assert "sparring partner" in captured["system"].lower()
    assert "mem!" in captured["system"]
    assert "Current setup" in captured["system"]
    assert "SPOT: 24042.0" in captured["system"]
    # The conversation history is passed through unchanged.
    assert captured["history"] == history


def test_spar_turn_passes_image_blocks_through():
    """A multimodal user turn (text + image block) reaches the completer intact."""
    captured = {}

    def fake(system, history):
        captured["history"] = history
        return "I read the chain: PCR 0.78, call wall 24,000 — pinned. STAND DOWN."

    history = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "Here's the option chain — what do you see?"},
            {"type": "image", "source": {"type": "base64",
             "media_type": "image/png", "data": "QUJD"}},
        ],
    }]
    reply = spar_turn(history, _snapshot(), _proposal(), completer=fake)
    assert "PCR" in reply
    # The image block is forwarded unchanged (vision passthrough).
    blocks = captured["history"][0]["content"]
    assert any(b["type"] == "image" for b in blocks)
