"""Agent layer — prompt assembly, the injectable Claude read, and the learning
memory distillation. All offline: the Anthropic call is stubbed.
"""

from __future__ import annotations

from types import SimpleNamespace

from analysis.proposal import TradeProposal, Recommendation
from agent.read import claude_read, ClaudeRead
from agent.prompt import build_system, build_user
from agent.memory import distill_memory


def _snapshot():
    read = {
        "mtf_call": "long", "regime_45_daily": 1, "supertrend_3m": 1,
        "ema5_trigger_3m": 1,
        "levels": {"ema_45": 23850.0, "supertrend": 23820.0,
                   "cpr_pivot": 23880.0, "cpr_tc": 23960.0, "cpr_bc": 23800.0},
    }
    return SimpleNamespace(
        instrument="NIFTY", ts="2026-06-19T15:00:00+05:30", spot=23900.0,
        chart_read=read,
        oi={"pcr": 0.9, "call_wall": {"strike": 24000}, "put_shelf": {"strike": 23800},
            "max_pain": 23900},
        macro=None, notes=[],
    )


def _proposal():
    return TradeProposal(
        instrument="NIFTY", trade_type="trade1", ts="2026-06-19T15:00:00+05:30",
        direction="long", entry=23900.0, stop=23880.0, target=23960.0, size_lots=75,
        vehicle="NIFTY 23600 CE (deep-ITM)", rupee_risk=112500.0, rr_ratio=3.0,
        recommendation=Recommendation.ENTER,
        checklist={"edge": "x", "stop": "x", "size": "x", "invalidation": "x",
                   "target": "x", "time_container": "x"},
        reasons=["MTF call: long", "OI: PCR 0.90"],
    )


def test_build_system_includes_constitution_and_memory():
    sys = build_system("Decisions logged: 3.")
    assert "sparring partner" in sys.lower()
    assert "SIX-LINE CHECK" in sys
    assert "Decisions logged: 3." in sys


def test_build_user_carries_snapshot_and_proposal_facts():
    user = build_user(_snapshot(), _proposal())
    assert "NIFTY" in user and "23900" in user
    assert "MTF call: long" in user
    assert "call wall 24000" in user
    assert "Recommendation: enter" in user
    assert "23600 CE" in user


def test_claude_read_uses_injected_completer():
    captured = {}

    def fake(system, user):
        captured["system"] = system
        captured["user"] = user
        return ClaudeRead(
            agrees_with_engine=True, chart_analysis="up-regime intact",
            oi_analysis="PCR 0.9, call wall 24000", where_moving="grind to 24000",
            right_trade="deep-ITM CE, stop 23880",
            challenge="watch the 24,000 wall — don't size up", recommendation="enter",
            confidence=4, key_risk="close below 23,880",
        )

    read = claude_read(_snapshot(), _proposal(), memory_text="mem!", completer=fake)
    assert read.enter and read.confidence == 4
    assert read.oi_bias == "neutral"             # default when the read omits it
    assert "mem!" in captured["system"]          # learning memory injected
    assert "spot 23900" not in captured["user"]  # sanity: format check below
    assert "SPOT: 23900.0" in captured["user"]


def test_claude_read_parses_oi_bias():
    def fake(system, user):
        return ClaudeRead(
            agrees_with_engine=True, chart_analysis="c", oi_analysis="o",
            where_moving="w", right_trade="r", challenge="ch", recommendation="enter",
            confidence=4, key_risk="k", oi_bias="bullish",
        )
    read = claude_read(_snapshot(), _proposal(), completer=fake)
    assert read.oi_bias == "bullish"


def test_build_user_carries_momentum_and_oi_bias_ask():
    user = build_user(_snapshot(), _proposal())
    assert "RSI(14)" in user and "MACD hist" in user      # full stack fed to Claude
    assert "oi_bias" in user                              # the +1 seam is requested


def test_distill_memory_summary():
    decisions = [
        {"decision": "rejected", "proposal": {"recommendation": "stand_down",
            "instrument": "NIFTY", "direction": "flat", "ts": "t1",
            "reasons": ["No edge: read is flat/conflicted — STAND DOWN."]}},
        {"decision": "approved", "proposal": {"recommendation": "enter",
            "instrument": "NIFTY", "direction": "long", "ts": "t2", "reasons": []}},
        {"decision": "rejected", "proposal": {"recommendation": "stand_down",
            "instrument": "NIFTY", "direction": "flat", "ts": "t3",
            "reasons": ["No edge: read is flat/conflicted — STAND DOWN."]}},
    ]
    mem = distill_memory(decisions)
    assert "Decisions logged: 3 (approved 1, rejected 2)" in mem
    assert "STAND_DOWN 2" in mem
    assert "No edge" in mem            # recurring reason surfaced
    assert "t3 NIFTY flat" in mem      # recent decisions listed


def test_distill_memory_empty():
    assert "early session" in distill_memory([])
