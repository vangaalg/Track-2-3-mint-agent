"""Free Telegram push — pure send/decision helpers (injected transport, no network)."""

from __future__ import annotations

from feeds import notify


def test_send_telegram_posts_via_transport():
    seen = {}
    def transport(url, data):
        seen["url"], seen["data"] = url, data
        return True
    ok = notify.send_telegram("TOK", "42", "hello", transport=transport)
    assert ok is True
    assert seen["url"] == "https://api.telegram.org/botTOK/sendMessage"
    assert seen["data"]["chat_id"] == "42" and seen["data"]["text"] == "hello"


def test_send_telegram_false_without_creds_or_on_error():
    assert notify.send_telegram(None, "42", "x") is False
    assert notify.send_telegram("TOK", None, "x") is False
    boom = lambda url, data: (_ for _ in ()).throw(RuntimeError("network"))
    assert notify.send_telegram("TOK", "42", "x", transport=boom) is False   # never raises


def test_telegram_from_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert notify.telegram_from_env() is None                    # unset -> disabled
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOK")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    sent = []
    fn = notify.telegram_from_env(transport=lambda u, d: sent.append(d) or True)
    assert fn is not None and fn("ping") is True and sent[0]["text"] == "ping"


def test_strong_bias_gating():
    assert notify.strong_bias({"bias": "bearish", "strength": 0.6, "insufficient": False}) == "bearish"
    assert notify.strong_bias({"bias": "bullish", "strength": 0.9, "insufficient": False}) == "bullish"
    assert notify.strong_bias({"bias": "bearish", "strength": 0.2, "insufficient": False}) is None  # weak
    assert notify.strong_bias({"bias": "neutral", "strength": 0.9, "insufficient": False}) is None
    assert notify.strong_bias({"insufficient": True}) is None
    assert notify.strong_bias(None) is None


def test_buildup_alert_text():
    txt = notify.buildup_alert_text("NIFTY", {"bias": "bearish", "score": -0.55,
                                              "call_writing": 5e5, "put_writing": 1e5})
    assert "NIFTY" in txt and "CLEAR BEARISH" in txt and "-0.55" in txt
