"""Best-effort GIFT Nifty fetch (overnight gap/direction) from investing.com.

GIFT Nifty (ex-SGX Nifty) trades while India is closed, so its last mark + change is the trader's
best read on the opening gap. There's no reliable free API, so this is a best-effort scrape:
- ``parse_gift(html)`` is a PURE parser (unit-tested against a saved snippet) — no network.
- ``fetch_gift(get=...)`` does the live pull behind an injectable ``get`` so it's testable offline.

investing.com fronts Cloudflare and may block a server-side request → ``fetch_gift`` returns None
and the recorder falls back to the trader's MANUAL value (the source of truth). Returns the
``feeds.macro.summarise_quote`` shape ``{price, change_pct}`` so it slots straight into the macro dict.
"""

from __future__ import annotations

import re

GIFT_URL = "https://www.investing.com/indices/gift-nifty"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}
_NUM = r"[-+]?[\d,]+\.?\d*"


def _f(s):
    try:
        return float(str(s).replace(",", "").replace("+", ""))
    except (TypeError, ValueError):
        return None


def parse_gift(html: str) -> dict | None:
    """Pull ``{price, change_pct}`` from an investing.com GIFT-Nifty page.

    Tries the JSON-ish ``data-test`` attributes investing.com uses for the last price and the
    percent change; returns None if neither is found (markup changed / blocked page).
    """
    if not html:
        return None
    price = None
    m = re.search(r'data-test="instrument-price-last"[^>]*>\s*(' + _NUM + r')', html)
    if m:
        price = _f(m.group(1))
    chg = None
    m = re.search(r'data-test="instrument-price-change-percent"[^>]*>\s*\(?\s*(' + _NUM + r')\s*%', html)
    if m:
        chg = _f(m.group(1))
    if price is None:
        return None
    return {"price": price, "change_pct": chg}


def fetch_gift(get=None, url: str = GIFT_URL) -> dict | None:
    """Live best-effort GIFT Nifty pull. ``get(url, headers)`` is injectable (default requests).
    Any failure / block degrades to None so the recorder uses the manual value instead."""
    try:
        if get is None:
            import requests
            get = lambda u, headers: requests.get(u, headers=headers, timeout=15)
        resp = get(url, _HEADERS)
        html = getattr(resp, "text", resp)
        return parse_gift(html)
    except Exception:
        return None
