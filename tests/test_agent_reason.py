"""agent.reason — the post-outcome reason-why (offline, mocked completer)."""

from __future__ import annotations

from agent.reason import explain_outcome, build_reason_user, ReasonWhy


def _ctx(**over):
    base = {
        "instrument": "NIFTY", "ts": "2026-06-19T14:18:00+05:30", "direction": "long",
        "entry": 23965.45, "stop": 23900.0, "target": 24050.0, "action": "take",
        "trigger_label": "genuine",
        "outcome": {"status": "win", "points": 84.5, "exit": 24050.0},
        "chart_read": {"mtf_call": "long", "regime_45_daily": 1, "supertrend_3m": 1,
                       "mtf_confidence": 3, "numbers": {"rsi_14": 61.0, "ema_5": 23975.6}},
    }
    base.update(over)
    return base


def test_build_reason_user_renders_context():
    u = build_reason_user(_ctx())
    assert "RESOLVED" in u and "long" in u and "23965.45" in u
    assert "win" in u and "84.5" in u            # the outcome
    assert "genuine" in u                         # the trader's label
    assert "RSI 61.0" in u                        # the chart numbers


def test_explain_outcome_uses_completer():
    captured = {}

    def completer(system, user):
        captured["system"], captured["user"] = system, user
        return ReasonWhy(why="held the 45-EMA and ran to target",
                         trigger_quality="genuine", lesson="clean close below the 5-EMA is the tell")

    rw = explain_outcome(_ctx(), memory_text="MEM", completer=completer)
    assert isinstance(rw, ReasonWhy) and rw.genuine and rw.trigger_quality == "genuine"
    assert "45-EMA" in rw.why and "tell" in rw.lesson
    assert "MEM" in captured["system"]            # memory injected into the system prompt
    assert "RESOLVED" in captured["user"]


def test_reasonwhy_false_is_not_genuine():
    rw = ReasonWhy(why="lucky bounce off a graze", trigger_quality="false", lesson="skip grazes")
    assert not rw.genuine
