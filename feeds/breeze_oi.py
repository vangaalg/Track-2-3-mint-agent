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


# Candidate Breeze field names for per-strike traded VOLUME and CHANGE-IN-OI. The
# exact keys `get_option_chain_quotes` returns aren't verifiable in the egress-locked
# sandbox, so we read the first candidate present and degrade to None otherwise. Fix
# these to the real keys once confirmed on a live pull (the rest of the code already
# handles the columns being present or all-None).
_VOLUME_KEYS = ("total_quantity_traded", "volume", "traded_quantity", "ttq", "quantity_traded")
_OI_CHANGE_KEYS = ("oi_change", "open_interest_change", "change_in_oi", "oi_chng")
_LTP_CHANGE_KEYS = ("ltp_change", "change", "ltp_chng", "chng", "net_change")


def _first(r: dict, keys) -> float | None:
    for k in keys:
        if k in r and r[k] not in (None, ""):
            v = _f(r[k])
            if v is not None:
                return v
    return None


def merge_chain(call_rows: list[dict], put_rows: list[dict]) -> pd.DataFrame:
    """Merge Breeze call/put rows into the canonical chain frame.

    Columns: ``strike, call_oi, put_oi, call_ltp, put_ltp`` (always) plus
    ``call_vol/put_vol`` (traded volume) and ``call_oi_chg/put_oi_chg`` (change in OI)
    when Breeze exposes them — all-None when it doesn't, so downstream stays safe. The
    OI/LTP feed the analysis + visualization; volume + ΔOI power the buildup read (a
    direct ΔOI field lets it classify from a SINGLE snapshot, no baseline needed).
    """
    def _index(rows):
        out = {}
        for r in rows:
            try:
                k = float(r["strike_price"])
            except (KeyError, TypeError, ValueError):
                continue
            out[k] = (float(r.get("open_interest") or 0), _f(r.get("ltp")),
                      _first(r, _VOLUME_KEYS), _first(r, _OI_CHANGE_KEYS),
                      _first(r, _LTP_CHANGE_KEYS))
        return out

    calls, puts = _index(call_rows), _index(put_rows)
    strikes = sorted(set(calls) | set(puts))
    blank = (0.0, None, None, None, None)
    return pd.DataFrame(
        {
            "strike": strikes,
            "call_oi": [calls.get(s, blank)[0] for s in strikes],
            "put_oi": [puts.get(s, blank)[0] for s in strikes],
            "call_ltp": [calls.get(s, blank)[1] for s in strikes],
            "put_ltp": [puts.get(s, blank)[1] for s in strikes],
            "call_vol": [calls.get(s, blank)[2] for s in strikes],
            "put_vol": [puts.get(s, blank)[2] for s in strikes],
            "call_oi_chg": [calls.get(s, blank)[3] for s in strikes],
            "put_oi_chg": [puts.get(s, blank)[3] for s in strikes],
            "call_ltp_chg": [calls.get(s, blank)[4] for s in strikes],
            "put_ltp_chg": [puts.get(s, blank)[4] for s in strikes],
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


# --- Breeze (ISEC) stock-code resolution ----------------------------------- #
# Breeze does NOT accept NSE symbols for most underlyings: Reliance trades as
# RELIND, HDFC Bank as HDFBAN, Bank Nifty as CNXBAN, etc. Live /healthz showed
# every mis-coded stock failing with "Error while calling service". Breeze's own
# ``get_names`` API maps an NSE symbol to the ISEC code at runtime, so we resolve
# lazily on first failure and CACHE the working code — no 50-symbol hardcoding.
# Index underlyings can't be resolved via get_names, so they carry a candidates
# list (FINNIFTY's provisional code failed live; NIFFIN/CNXFIN are the usual ICICI
# spellings — first one that returns data wins and is cached).
_CODE_CACHE: dict[str, str] = {}
_INDEX_CANDIDATES = {"FINNIFTY": ["NIFFIN", "CNXFIN"]}


def resolve_stock_code(client, nse_symbol: str) -> str | None:
    """The ISEC stock code for an NSE symbol via Breeze's ``get_names`` (None on any
    failure — callers fall back to the input symbol)."""
    try:
        resp = client.get_names(exchange_code="NSE", stock_code=nse_symbol)
        if isinstance(resp, dict):
            for k in ("isec_stock_code", "isec_code", "stock_code"):
                v = resp.get(k)
                if v and str(v).strip() and str(v).strip().upper() != nse_symbol.upper():
                    return str(v).strip()
            # some SDK versions nest under Success
            inner = resp.get("Success") or {}
            if isinstance(inner, dict):
                for k in ("isec_stock_code", "isec_code", "stock_code"):
                    v = inner.get(k)
                    if v and str(v).strip():
                        return str(v).strip()
        else:                                   # object-style response
            for k in ("isec_stock_code", "isec_code", "stock_code"):
                v = getattr(resp, k, None)
                if v and str(v).strip():
                    return str(v).strip()
    except Exception:
        return None
    return None


def make_chain_fetcher(
    expiry=None, weekday: int = 3, exchange: str = "NFO", client_factory=None,
    monthly: bool = False,
):
    """Return ``fetch(instrument) -> chain DataFrame`` for ``feeds.oi.fetch_oi``.

    Pulls calls and puts for the (config-driven) expiry and merges them. Set
    ``monthly=True`` for underlyings with NO weekly options (Bank Nifty, Sensex,
    single stocks) so the expiry resolves to the month's last expiry-weekday instead
    of the next weekly one (a weekly date returns "No Data Found").

    SELF-HEALING symbol resolution: the pull is tried with the given (or cached)
    code first; on a Breeze error it retries with the ``get_names``-resolved ISEC
    code (stocks) or the known index candidates (FINNIFTY), caching whichever code
    returns data. Any final failure propagates to ``fetch_oi`` → panel degrades.
    """
    def _pull(client, code: str, exp: str) -> pd.DataFrame:
        kw = dict(stock_code=code, exchange_code=exchange,
                  product_type="options", expiry_date=exp)
        calls = client.get_option_chain_quotes(right="call", **kw)
        puts = client.get_option_chain_quotes(right="put", **kw)
        return merge_chain(_rows(calls), _rows(puts))

    def fetch(instrument: str) -> pd.DataFrame:
        client = (client_factory or get_breeze_client)()
        exp = _expiry_iso(expiry, weekday, monthly)
        code = _CODE_CACHE.get(instrument, instrument)
        try:
            return _pull(client, code, exp)
        except RuntimeError as first_err:
            alts: list[str] = []
            if instrument in _INDEX_CANDIDATES:
                alts = [c for c in _INDEX_CANDIDATES[instrument] if c != code]
            else:
                rc = resolve_stock_code(client, instrument)
                if rc and rc != code:
                    alts = [rc]
            for alt in alts:
                try:
                    df = _pull(client, alt, exp)
                    _CODE_CACHE[instrument] = alt      # self-heal: remember what works
                    return df
                except RuntimeError:
                    continue
            raise first_err

    return fetch
