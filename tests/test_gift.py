"""feeds.gift — pure investing.com parser + injectable fetch (offline)."""

from __future__ import annotations

from feeds.gift import parse_gift, fetch_gift

_HTML = ('<div data-test="instrument-price-last">24,135.50</div>'
         '<span data-test="instrument-price-change-percent">(+0.42%)</span>')


def test_parse_gift_extracts_price_and_change():
    out = parse_gift(_HTML)
    assert out["price"] == 24135.5 and out["change_pct"] == 0.42


def test_parse_gift_none_on_blocked_or_empty():
    assert parse_gift("<html>Just a moment… (Cloudflare)</html>") is None
    assert parse_gift("") is None


def test_fetch_gift_injected_get_and_graceful_degrade():
    class R:
        text = _HTML
    assert fetch_gift(get=lambda u, headers: R())["price"] == 24135.5

    def boom(u, headers):
        raise RuntimeError("blocked")
    assert fetch_gift(get=boom) is None                # block/failure → None (manual fallback)
