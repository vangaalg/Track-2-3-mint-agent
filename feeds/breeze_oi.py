"""Live option-chain fetcher for ``feeds.oi`` — Breeze ``get_option_chain_quotes``.

Produces the ``{strike, call_oi, put_oi}`` frame that ``feeds.oi.summarise_chain``
reduces to PCR / call-wall / put-shelf / max-pain. The merge/parse logic is pure
(unit-tested with a mocked SDK response); the live pull runs on the user's machine.

NSE's Nifty weekly-expiry weekday has shifted before, so the expiry is config —
pass an explicit ``expiry`` or set ``weekday`` (default Thursday); verify locally.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from loaders.breeze import get_breeze_client


def merge_chain(call_rows: list[dict], put_rows: list[dict]) -> pd.DataFrame:
    """Merge Breeze call/put rows into the canonical chain frame.

    Columns: ``strike, call_oi, put_oi, call_ltp, put_ltp`` — OI for the analysis
    layer, LTP for the per-strike visualization.
    """
    def _index(rows):
        out = {}
        for r in rows:
            try:
                k = float(r["strike_price"])
            except (KeyError, TypeError, ValueError):
                continue
            out[k] = (float(r.get("open_interest") or 0), _f(r.get("ltp")))
        return out

    calls, puts = _index(call_rows), _index(put_rows)
    strikes = sorted(set(calls) | set(puts))
    return pd.DataFrame(
        {
            "strike": strikes,
            "call_oi": [calls.get(s, (0.0, None))[0] for s in strikes],
            "put_oi": [puts.get(s, (0.0, None))[0] for s in strikes],
            "call_ltp": [calls.get(s, (0.0, None))[1] for s in strikes],
            "put_ltp": [puts.get(s, (0.0, None))[1] for s in strikes],
        }
    )


def _f(x):
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


def nearest_weekly(weekday: int = 3, today: date | None = None) -> date:
    """Next date falling on ``weekday`` (Mon=0..Sun=6); Thursday=3 by default."""
    today = today or date.today()
    return today + timedelta(days=(weekday - today.weekday()) % 7)


def _last_weekday_of_month(weekday: int, year: int, month: int) -> date:
    """The last date in ``year``/``month`` that falls on ``weekday`` (Mon=0..Sun=6)."""
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def nearest_monthly(weekday: int = 1, today: date | None = None) -> date:
    """The nearest MONTHLY expiry on/after ``today`` = the last ``weekday`` of the
    month (rolling to next month once this month's has passed).

    NSE removed weekly options for Bank Nifty (and most non-Nifty underlyings) — they
    expire only on the month's last expiry-weekday. ``weekday`` is config because NSE
    has shifted it (Bank Nifty = last Tuesday currently)."""
    today = today or date.today()
    cand = _last_weekday_of_month(weekday, today.year, today.month)
    if cand < today:                       # this month's expiry already passed
        y, m = (today.year + (today.month == 12)), (today.month % 12 + 1)
        cand = _last_weekday_of_month(weekday, y, m)
    return cand


def _expiry_iso(expiry, weekday: int, monthly: bool = False) -> str:
    """Breeze expiry ISO ``YYYY-MM-DDT06:00:00.000Z`` from a date/str/None.

    ``monthly=True`` resolves to the month's last expiry-weekday (for underlyings with
    no weekly options, e.g. Bank Nifty); otherwise the next weekly ``weekday``."""
    if isinstance(expiry, str):
        return expiry
    if isinstance(expiry, date):
        d = expiry
    else:
        d = nearest_monthly(weekday) if monthly else nearest_weekly(weekday)
    return datetime(d.year, d.month, d.day, 6, 0, 0).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _rows(resp: dict) -> list[dict]:
    if resp.get("Error"):
        raise RuntimeError(f"Breeze option-chain error: {resp.get('Error')}")
    return resp.get("Success") or []


def make_chain_fetcher(
    expiry=None, weekday: int = 3, exchange: str = "NFO", client_factory=None,
    monthly: bool = False,
):
    """Return ``fetch(instrument) -> chain DataFrame`` for ``feeds.oi.fetch_oi``.

    Pulls calls and puts for the (config-driven) expiry and merges them. Set
    ``monthly=True`` for underlyings with NO weekly options (Bank Nifty, Sensex,
    single stocks) so the expiry resolves to the month's last expiry-weekday instead
    of the next weekly one (a weekly date returns "No Data Found"). Any failure
    propagates to ``fetch_oi``, which degrades the OI panel to None.
    """
    def fetch(instrument: str) -> pd.DataFrame:
        client = (client_factory or get_breeze_client)()
        exp = _expiry_iso(expiry, weekday, monthly)
        kw = dict(stock_code=instrument, exchange_code=exchange,
                  product_type="options", expiry_date=exp)
        calls = client.get_option_chain_quotes(right="call", **kw)
        puts = client.get_option_chain_quotes(right="put", **kw)
        return merge_chain(_rows(calls), _rows(puts))

    return fetch
