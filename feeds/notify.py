"""Free phone push via the Telegram Bot API — no dependency, no cost.

The always-on recorder calls this to push a phone alert when an OI-buildup lean turns
CLEAR (strong bull/bear), so the trader is notified even with the cockpit tab closed
(the in-app beep only fires while the tab is open). Telegram bots are free; the bot
token + chat id come from the env — unset => disabled (a silent no-op), so this is safe
to ship before it's configured.

Setup (one-time, on the phone):
  1. Message @BotFather -> /newbot -> copy the bot TOKEN it returns.
  2. Message @userinfobot -> copy your numeric Id (the CHAT ID).
  3. Send your new bot any message once (a bot can't DM you until you've DM'd it).
  4. On Railway set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.

The HTTP call is injectable (``transport``) so the alert path is unit-tested offline
with no network and no real bot. Nothing here ever raises into the recorder loop.
"""

from __future__ import annotations

import os
import urllib.parse
import urllib.request

STRONG = 0.4      # |score| for a CLEAR lean (mirrors analysis.trade1.BUILDUP_STRONG + the UI)


def _post(url: str, data: dict, timeout: float = 10.0) -> bool:
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(data).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:      # nosec - fixed telegram host
        return 200 <= getattr(r, "status", 200) < 300


def send_telegram(token: str | None, chat_id: str | None, text: str,
                  *, transport=None) -> bool:
    """POST one message to Telegram. Never raises — returns True on success, else False."""
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": str(chat_id), "text": text, "disable_web_page_preview": "true"}
    try:
        return bool((transport or _post)(url, data))
    except Exception:
        return False


def telegram_from_env(transport=None):
    """A ``notify(text) -> bool`` bound to TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID, or None
    when either is unset (so callers can treat notifications as simply disabled)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return None
    return lambda text: send_telegram(token, chat_id, text, transport=transport)


def strong_bias(signal: dict | None, threshold: float = STRONG) -> str | None:
    """The CLEAR lean worth a push (``"bullish"``/``"bearish"``), or None. Pure."""
    if not signal or signal.get("insufficient"):
        return None
    if float(signal.get("strength") or 0) < threshold:
        return None
    b = signal.get("bias")
    return b if b in ("bullish", "bearish") else None


def buildup_alert_text(symbol: str, signal: dict) -> str:
    """The phone message for a CLEAR-lean event."""
    b = (signal.get("bias") or "").upper()
    return (f"⚠ {symbol} OI → CLEAR {b} (score {signal.get('score')})\n"
            f"call writing {signal.get('call_writing')} · "
            f"put writing {signal.get('put_writing')}")
