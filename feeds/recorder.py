"""Standalone market-hours recorder — the forward OI/macro flywheel.

Historical intraday OI can't be bought back (Breeze caps it; vendors cost money), so
we accumulate it live from now on. Each cycle, during market hours, this records per
instrument: the full option chain (-> feeds.oi_store) + a compact PCR/max-pain/levels
summary row (-> feeds.oi_summary_store), plus the macro scorecard once (-> feeds.macro_store).

Cadence: indices every 15 min, stocks every 60 min (the trader's call). Scope starts
with NIFTY + Bank Nifty (proven Breeze calls); Sensex (BSE) + the Nifty-50 stocks are
listed but opt-in until verified — a failing instrument is logged and never blocks the
others.

The CORE (`record_once`) + session guard (`in_session`) are pure with injectable
fetchers so they test offline; `run()` does the live Breeze pulls on the user's machine.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import time as dtime
from pathlib import Path

import pandas as pd

try:                                                  # tz only needed for live/run
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:                                     # pragma: no cover
    IST = None

from feeds import oi_store, oi_summary_store, macro_store
from feeds.oi import summarise_chain
from feeds.oi_levels import wall_levels, scaled_offsets

OPEN, CLOSE = dtime(9, 15), dtime(15, 30)

# Nifty-50 constituents (NSE symbols; some Breeze codes differ → degrade + log).
NIFTY50_STOCKS = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "ITC", "LT", "AXISBANK",
    "SBIN", "BHARTIARTL", "KOTAKBANK", "HINDUNILVR", "BAJFINANCE", "ASIANPAINT",
    "MARUTI", "TITAN", "SUNPHARMA", "TATAMOTORS", "NTPC", "POWERGRID", "ULTRACEMCO",
    "NESTLEIND", "WIPRO", "TATASTEEL", "HCLTECH", "ADANIENT", "ADANIPORTS", "JSWSTEEL",
    "M&M", "BAJAJFINSV", "ONGC", "COALINDIA", "GRASIM", "HINDALCO", "INDUSINDBK",
    "TECHM", "CIPLA", "DRREDDY", "BRITANNIA", "EICHERMOT", "APOLLOHOSP", "BPCL",
    "DIVISLAB", "HEROMOTOCO", "TATACONSUM", "BAJAJ-AUTO", "SBILIFE", "HDFCLIFE",
    "LTIM", "SHRIRAMFIN",
]

# Default scope: indices first. weekday = expiry weekday (verify live; NSE NIFTY = Tue=1).
DEFAULT_INSTRUMENTS = [
    {"name": "NIFTY", "symbol": "NIFTY", "exchange": "NFO", "klass": "index",
     "weekday": 1, "band": [37.0, 72.0]},
    {"name": "BANKNIFTY", "symbol": "CNXBAN", "exchange": "NFO", "klass": "index",
     "weekday": 1, "band": "scale"},
    {"name": "SENSEX", "symbol": "SENSEX", "exchange": "BFO", "klass": "index",
     "weekday": 4, "band": "scale", "enabled": False},      # BSE — verify before enabling
]


# --------------------------------------------------------------------------- #
# Pure core
# --------------------------------------------------------------------------- #
def in_session(now) -> bool:
    """True on a weekday between 09:15 and 15:30 IST."""
    t = pd.Timestamp(now)
    t = t.tz_localize(IST) if t.tzinfo is None and IST else (t.tz_convert(IST) if t.tzinfo else t)
    return t.weekday() < 5 and OPEN <= t.time() <= CLOSE


def implied_spot(chain: pd.DataFrame):
    """Chain-implied spot via put-call parity at the ATM (min |call_ltp − put_ltp|).

    Used only as a fallback when a live spot quote isn't available; degrades to the
    median strike if LTPs are absent.
    """
    try:
        c = chain.dropna(subset=["call_ltp", "put_ltp"])
        if c.empty:
            return float(chain["strike"].median())
        row = c.loc[(c["call_ltp"] - c["put_ltp"]).abs().idxmin()]
        return float(row["strike"] + (row["call_ltp"] - row["put_ltp"]))
    except Exception:
        return None


def _offsets_for(inst: dict, spot) -> list[float]:
    band = inst.get("band", "scale")
    if isinstance(band, (list, tuple)):
        return [float(x) for x in band]
    return scaled_offsets(spot)                       # "scale" → price-scaled from NIFTY


def _roots(root):
    """Resolve the three store roots from one optional base dir (for tests)."""
    if root is None:
        return oi_store.DATA_DIR, None, None
    r = Path(root)
    return r / "oi", r / "oi_summary", r / "macro"


def record_once(instruments, fetchers, spot_fns=None, macro_fn=None,
                now=None, root=None, errors=None, pace_s: float = 0.0) -> dict:
    """Record ONE cycle for the given instruments + macro. Pure given injected fns.

    ``fetchers``: {name: fetch(symbol)->chain_df}; ``spot_fns``: {name: spot(symbol)->float}
    (optional, falls back to chain-implied); ``macro_fn``: ()->macro dict (optional).
    Every instrument is independent — a failure is captured in ``errors`` and the rest
    continue, so an untested Sensex/stock never blocks NIFTY. Returns
    ``{"saved": [...names...], "macro": bool}``.
    """
    spot_fns = spot_fns or {}
    now = now or (pd.Timestamp.now(tz=IST) if IST else pd.Timestamp.now())
    oi_base, summary_root, macro_root = _roots(root)
    saved = []
    for i, inst in enumerate(instruments):
        name, symbol = inst["name"], inst.get("symbol", inst["name"])
        try:
            chain = fetchers[name](symbol)
            if chain is None or getattr(chain, "empty", True):
                raise RuntimeError("empty chain")
            spot = None
            if name in spot_fns:
                try:
                    spot = float(spot_fns[name](symbol))
                except Exception:
                    spot = None
            if spot is None:
                spot = implied_spot(chain)
            summary = summarise_chain(chain, spot)
            levels = wall_levels(summary, _offsets_for(inst, spot))
            oi_store.save_chain(name, now, spot, chain, base=oi_base)
            oi_summary_store.append_summary(name, now, spot, summary, levels, root=summary_root)
            saved.append(name)
        except Exception as exc:
            if errors is not None:
                errors.append(f"{name}: {exc}")
        if pace_s and i < len(instruments) - 1:
            time.sleep(pace_s)
    macro_ok = False
    if macro_fn is not None:
        try:
            macro_store.append_macro(macro_fn(), now, root=macro_root)
            macro_ok = True
        except Exception as exc:
            if errors is not None:
                errors.append(f"macro: {exc}")
    return {"saved": saved, "macro": macro_ok}


def select_instruments(names=None, with_stocks=False) -> list[dict]:
    """Build the instrument list: enabled defaults, optional name subset + stocks."""
    insts = [i for i in DEFAULT_INSTRUMENTS if i.get("enabled", True)]
    if with_stocks:
        insts += [{"name": s, "symbol": s, "exchange": "NFO", "klass": "stock",
                   "weekday": 3, "band": "scale", "monthly": True} for s in NIFTY50_STOCKS]
    if names:
        want = {n.strip().upper() for n in names}
        insts = [i for i in insts if i["name"].upper() in want]
    return insts


# --------------------------------------------------------------------------- #
# Live wiring (runs on the user's machine — networked)
# --------------------------------------------------------------------------- #
def _build_live(instruments):
    """Build {name: chain_fetcher} + {name: spot_fn} + macro_fn from Breeze/TD."""
    from feeds.breeze_oi import make_chain_fetcher
    from feeds.macro import fetch_macro
    from feeds.td_macro import make_quote_fn, SCORECARD_SYMBOLS
    from loaders.breeze import get_breeze_client

    def make_spot_fn(exchange_code):
        def spot(symbol):
            client = get_breeze_client()
            resp = client.get_quotes(stock_code=symbol, exchange_code=exchange_code,
                                     product_type="cash")
            if resp.get("Error"):
                raise RuntimeError(resp["Error"])
            return float((resp.get("Success") or [{}])[0]["ltp"])
        return spot

    fetchers, spot_fns = {}, {}
    for inst in instruments:
        fetchers[inst["name"]] = make_chain_fetcher(weekday=inst["weekday"],
                                                    exchange=inst["exchange"])
        spot_fns[inst["name"]] = make_spot_fn("BSE" if inst["exchange"] == "BFO" else "NSE")
    qf = make_quote_fn()
    macro_fn = lambda: fetch_macro(SCORECARD_SYMBOLS, qf)
    return fetchers, spot_fns, macro_fn


def run(instruments=None, index_every=15, stock_every=60, poll_s=30, pace_s=0.3,
        on_cycle=None) -> None:
    """Market-hours loop: record each instrument on its cadence, macro each cycle.

    ``on_cycle(info)`` is called after each recording cycle with
    ``{ts, saved, macro, errors}`` (used by the deployed service to surface live status
    + trigger persistence); default None keeps the plain loop.
    """
    instruments = instruments or select_instruments()
    fetchers, spot_fns, macro_fn = _build_live(instruments)
    last = {}
    print(f"recorder: {[i['name'] for i in instruments]} | indices {index_every}m / "
          f"stocks {stock_every}m", file=sys.stderr)
    while True:
        now = pd.Timestamp.now(tz=IST)
        if not in_session(now):
            time.sleep(60)
            continue
        due = [i for i in instruments
               if (i["name"] not in last)
               or (now - last[i["name"]]).total_seconds()
               >= (index_every if i["klass"] == "index" else stock_every) * 60 - 1]
        if due:
            errors = []
            res = record_once(due, fetchers, spot_fns, macro_fn=macro_fn, now=now,
                              errors=errors, pace_s=pace_s)
            for n in res["saved"]:
                last[n] = now
            print(f"{now:%H:%M} saved={res['saved']} macro={res['macro']}"
                  + (f" ERR={errors}" if errors else ""), file=sys.stderr)
            if on_cycle is not None:
                try:
                    on_cycle({"ts": now.isoformat(), "saved": res["saved"],
                              "macro": res["macro"], "errors": errors})
                except Exception:
                    pass
        time.sleep(poll_s)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--once", action="store_true",
                    help="record a single cycle now (ignores market hours) — live smoke-test")
    ap.add_argument("--instruments", default=None,
                    help="comma-separated names to record (default: enabled defaults)")
    ap.add_argument("--stocks", action="store_true", help="also record the Nifty-50 stocks")
    ap.add_argument("--index-every", type=int, default=15, help="index cadence (minutes)")
    ap.add_argument("--stock-every", type=int, default=60, help="stock cadence (minutes)")
    args = ap.parse_args(argv)

    names = args.instruments.split(",") if args.instruments else None
    instruments = select_instruments(names, with_stocks=args.stocks)
    if not instruments:
        print("no instruments selected", file=sys.stderr)
        return 1
    if args.once:
        fetchers, spot_fns, macro_fn = _build_live(instruments)
        errors = []
        res = record_once(instruments, fetchers, spot_fns, macro_fn=macro_fn,
                          errors=errors, pace_s=0.3)
        print(f"saved={res['saved']} macro={res['macro']}", file=sys.stderr)
        if errors:
            print("errors:\n  " + "\n  ".join(errors), file=sys.stderr)
        return 0
    run(instruments, index_every=args.index_every, stock_every=args.stock_every)
    return 0


if __name__ == "__main__":
    sys.exit(main())
