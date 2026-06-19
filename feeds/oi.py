"""OI / option-chain summary for an index (Nifty).

The heavy lifting is a PURE function ``summarise_chain`` that turns an
option-chain frame into the handful of numbers the analysis layer needs (PCR,
the nearest call wall / put shelf, max-pain). The live fetch is injected so this
is fully testable offline; in production a Breeze option-chain pull (or the NSE
option-chain endpoint as a fallback) supplies the frame.

Chain frame contract (one row per strike):
    strike (float) · call_oi (float) · put_oi (float)
"""

from __future__ import annotations

from typing import Callable

import pandas as pd

_CHAIN_COLUMNS = ("strike", "call_oi", "put_oi")


def summarise_chain(chain: pd.DataFrame, spot: float) -> dict:
    """Reduce an option-chain frame to the OI context the read uses.

    Returns ``pcr`` (total put_oi / call_oi), ``call_wall`` / ``put_shelf`` (the
    strikes with the most call / put OI — the ceiling / floor), ``max_pain`` (the
    strike minimising total intrinsic payout to option buyers), and the ATM strike.
    """
    missing = [c for c in _CHAIN_COLUMNS if c not in chain.columns]
    if missing:
        raise ValueError(f"option chain missing columns {missing}; got {list(chain.columns)}")
    c = chain.sort_values("strike").reset_index(drop=True)

    pcr = float(c["put_oi"].sum() / c["call_oi"].sum()) if c["call_oi"].sum() else float("nan")

    call_wall = c.loc[c["call_oi"].idxmax()]
    put_shelf = c.loc[c["put_oi"].idxmax()]
    atm = float(c.iloc[(c["strike"] - spot).abs().idxmin()]["strike"])

    return {
        "pcr": pcr,
        "call_wall": {"strike": float(call_wall["strike"]), "oi": float(call_wall["call_oi"])},
        "put_shelf": {"strike": float(put_shelf["strike"]), "oi": float(put_shelf["put_oi"])},
        "max_pain": _max_pain(c),
        "atm": atm,
    }


def _max_pain(c: pd.DataFrame) -> float:
    """Strike at which the total intrinsic value owed to option buyers is least."""
    strikes = c["strike"].to_numpy()
    pain = []
    for k in strikes:
        call_pay = ((k - c["strike"]).clip(lower=0) * c["call_oi"]).sum()  # ITM calls if expiry=k
        put_pay = ((c["strike"] - k).clip(lower=0) * c["put_oi"]).sum()
        pain.append(call_pay + put_pay)
    return float(strikes[int(pd.Series(pain).idxmin())])


def fetch_oi(
    instrument: str, spot: float, fetch_fn: Callable[[str], pd.DataFrame] | None = None
) -> dict | None:
    """Fetch + summarise the option chain. Returns None (degrade) if no fetcher.

    ``fetch_fn(instrument) -> chain DataFrame`` is injected (Breeze option chain
    or NSE endpoint). Any failure degrades to None so the snapshot still builds.
    """
    if fetch_fn is None:
        return None
    try:
        chain = fetch_fn(instrument)
        return summarise_chain(chain, spot)
    except Exception:
        return None
