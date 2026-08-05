"""OI-buildup engine — change-in-OI × change-in-price, the LTPCalculator-style read.

Snapshot OI (``feeds.oi.summarise_chain``) gives PCR / walls / max-pain. What it
CANNOT tell you is whether those walls are being **built** or **unwound** right now —
that needs the change in OI over time. This module diffs a *current* chain snapshot
against a *baseline* (default = the session's first snapshot = day buildup, the usual
LTPCalculator basis) and classifies each strike with the standard buildup matrix:

    side  ΔOI  Δprice  state                    underlying bias
    ----  ---  ------  -----------------------  ---------------
    Call   +     +     long buildup             bullish
    Call   +     −     writing / short buildup  bearish  (resistance)
    Call   −     +     short covering           bullish
    Call   −     −     long unwinding           bearish
    Put    +     −     writing / short buildup  bullish  (support)
    Put    +     +     long buildup             bearish
    Put    −     +     short covering           bearish
    Put    −     −     long unwinding           bullish

HYBRID moneyness rule (added after a live Sensibull comparison caught a miscall): the
price-based matrix above applies to **ITM** strikes only. At **OTM** strikes — where
index-option OI is overwhelmingly WRITER-driven — additions are classified ``writing``
regardless of the option's own price direction, and reductions as writers leaving
(call side → ``short_covering``/bullish, put side → ``long_unwinding``/bearish). The
pure price matrix misfired on trending mornings: an up-move lifts call premiums, so a
resistance wall of OTM call additions scored as bullish "long buildup" and pegged the
lean at a false CLEAR +1.00.

The per-strike states aggregate (ΔOI-weighted, ATM-windowed) into a single
buyer-vs-seller lean — ``bias`` (bullish/bearish/neutral) + a signed ``score`` and a
``strength``. That is the deterministic OI direction the agent lacked (today OI
direction is only Claude's subjective ``oi_bias``).

Pure + offline-testable. Δprice is the OPTION's own LTP change (a call's price vs its
OI, a put's price vs its OI) — the correct per-strike writing/unwinding basis. Where a
side has no quoted LTP the strike is marked ``unknown`` and left out of the tally
(honest, rather than guessing).

Change-in-OI needs ≥2 snapshots, so this is a LIVE / forward read off the OI flywheel
(``feeds.oi_store`` + the recorder); it is NOT historically backtestable except where
recorder data has already accrued. Callers surface ``insufficient=True`` cleanly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Canonical chain columns we diff (same contract as feeds.oi / feeds.oi_store).
_COLS = ("strike", "call_oi", "put_oi", "call_ltp", "put_ltp")

# Per-strike buildup states (side-neutral names; bias carries the direction).
LONG_BUILDUP = "long_buildup"
SHORT_BUILDUP = "writing"          # short buildup = option writing
SHORT_COVERING = "short_covering"
LONG_UNWINDING = "long_unwinding"
FLAT = "flat"
UNKNOWN = "unknown"

_EPS = 1e-9


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    if col in df.columns:
        return pd.to_numeric(df[col], errors="coerce")
    return pd.Series([np.nan] * len(df), index=df.index)


def _state(d_oi: float, d_px: float, is_call: bool, otm: bool = False) -> tuple[str, int]:
    """Return ``(state, bias)`` for one side of one strike. bias +1 bullish / -1 bearish.

    HYBRID rule (the Sensibull-comparison fix): for INDEX options, large OI changes at
    **OTM** strikes are overwhelmingly WRITER-driven — sellers building/lifting walls —
    so OTM additions are classified as ``writing`` REGARDLESS of the option's own price
    direction, and OTM reductions as writers leaving. The pure ΔOI×Δprice textbook matrix
    misfired on trending mornings: an up-move lifts call premiums, so heavy OTM call
    additions (a resistance wall) scored as bullish "long buildup" and pegged the lean at
    a false CLEAR 1.00. The price-based matrix still applies to ITM strikes (where
    buyer-driven flows are plausible)."""
    if not np.isfinite(d_oi):
        return UNKNOWN, 0
    if abs(d_oi) <= _EPS:
        return FLAT, 0
    oi_up = d_oi > 0
    if otm:                                        # writer-driven side of the book
        if oi_up:                                  # fresh OTM supply = a wall being built
            return SHORT_BUILDUP, (-1 if is_call else +1)
        if is_call:                                # call wall lifting = resistance easing
            return SHORT_COVERING, +1
        return LONG_UNWINDING, -1                  # put shelf thinning = support weakening
    if not np.isfinite(d_px):
        return UNKNOWN, 0
    px_up = d_px > _EPS
    px_dn = d_px < -_EPS
    if not (px_up or px_dn):                       # OI moved but price flat → ambiguous
        return FLAT, 0
    if oi_up and px_up:                            # fresh positions, price up
        state = LONG_BUILDUP
        bias = +1 if is_call else -1
    elif oi_up and px_dn:                          # fresh positions, price down = writing
        state = SHORT_BUILDUP
        bias = -1 if is_call else +1               # call writing bearish, put writing bullish
    elif (not oi_up) and px_up:                    # OI down, price up = short covering
        state = SHORT_COVERING
        bias = +1 if is_call else -1
    else:                                          # OI down, price down = long unwinding
        state = LONG_UNWINDING
        bias = -1 if is_call else +1
    return state, bias


def buildup_table(prev_chain: pd.DataFrame | None, cur_chain: pd.DataFrame | None,
                  spot: float, window: float = 1000.0) -> pd.DataFrame:
    """Per-strike ΔOI/Δprice + buildup state within ``±window`` of spot.

    Columns: ``strike, d_call_oi, d_put_oi, d_call_ltp, d_put_ltp, call_state,
    put_state, call_bias, put_bias``. Empty frame when there is nothing to diff.
    """
    empty = pd.DataFrame(columns=["strike", "d_call_oi", "d_put_oi", "d_call_ltp",
                                  "d_put_ltp", "call_state", "put_state",
                                  "call_bias", "put_bias"])
    if prev_chain is None or cur_chain is None or prev_chain.empty or cur_chain.empty:
        return empty
    if "strike" not in prev_chain.columns or "strike" not in cur_chain.columns:
        return empty

    prev = prev_chain.copy()
    cur = cur_chain.copy()
    prev["strike"] = pd.to_numeric(prev["strike"], errors="coerce")
    cur["strike"] = pd.to_numeric(cur["strike"], errors="coerce")
    cur = cur[(cur["strike"] - spot).abs() <= window]
    if cur.empty:
        return empty

    m = cur.merge(prev, on="strike", how="inner", suffixes=("", "_prev"))
    if m.empty:
        return empty
    m = m.sort_values("strike").reset_index(drop=True)

    d_call_oi = _num(m, "call_oi") - _num(m, "call_oi_prev")
    d_put_oi = _num(m, "put_oi") - _num(m, "put_oi_prev")
    d_call_ltp = _num(m, "call_ltp") - _num(m, "call_ltp_prev")
    d_put_ltp = _num(m, "put_ltp") - _num(m, "put_ltp_prev")

    strikes = m["strike"]
    call = [_state(o, p, True, otm=(k > spot))
            for o, p, k in zip(d_call_oi, d_call_ltp, strikes)]
    put = [_state(o, p, False, otm=(k < spot))
           for o, p, k in zip(d_put_oi, d_put_ltp, strikes)]
    return pd.DataFrame({
        "strike": m["strike"].astype("int64"),
        "d_call_oi": d_call_oi.to_numpy(),
        "d_put_oi": d_put_oi.to_numpy(),
        "d_call_ltp": d_call_ltp.to_numpy(),
        "d_put_ltp": d_put_ltp.to_numpy(),
        "call_state": [s for s, _ in call],
        "put_state": [s for s, _ in put],
        "call_bias": [b for _, b in call],
        "put_bias": [b for _, b in put],
    })


def has_direct_change(chain: pd.DataFrame | None) -> bool:
    """True when the chain already carries per-strike ΔOI + Δprice fields (Breeze's
    ``call_oi_chg``/``call_ltp_chg`` etc., captured by ``feeds.breeze_oi.merge_chain``)
    — then buildup classifies from a SINGLE snapshot, no baseline needed."""
    if chain is None or getattr(chain, "empty", True):
        return False
    need = ("call_oi_chg", "put_oi_chg", "call_ltp_chg", "put_ltp_chg")
    if not all(c in chain.columns for c in need):
        return False
    return bool(pd.to_numeric(chain["call_oi_chg"], errors="coerce").notna().any()
                or pd.to_numeric(chain["put_oi_chg"], errors="coerce").notna().any())


def buildup_table_from_change(chain: pd.DataFrame | None, spot: float,
                              window: float = 1000.0) -> pd.DataFrame:
    """Per-strike buildup table from the chain's OWN ΔOI/Δprice fields (one snapshot).

    Same output shape as ``buildup_table`` but sourced from Breeze's per-strike change
    columns instead of diffing two snapshots — so it works on the first pull of the day.
    """
    empty = pd.DataFrame(columns=["strike", "d_call_oi", "d_put_oi", "d_call_ltp",
                                  "d_put_ltp", "call_state", "put_state",
                                  "call_bias", "put_bias"])
    if not has_direct_change(chain):
        return empty
    c = chain.copy()
    c["strike"] = pd.to_numeric(c["strike"], errors="coerce")
    c = c[(c["strike"] - spot).abs() <= window].sort_values("strike").reset_index(drop=True)
    if c.empty:
        return empty
    d_call_oi, d_put_oi = _num(c, "call_oi_chg"), _num(c, "put_oi_chg")
    d_call_ltp, d_put_ltp = _num(c, "call_ltp_chg"), _num(c, "put_ltp_chg")
    strikes = c["strike"]
    call = [_state(o, p, True, otm=(k > spot))
            for o, p, k in zip(d_call_oi, d_call_ltp, strikes)]
    put = [_state(o, p, False, otm=(k < spot))
           for o, p, k in zip(d_put_oi, d_put_ltp, strikes)]
    return pd.DataFrame({
        "strike": c["strike"].astype("int64"),
        "d_call_oi": d_call_oi.to_numpy(), "d_put_oi": d_put_oi.to_numpy(),
        "d_call_ltp": d_call_ltp.to_numpy(), "d_put_ltp": d_put_ltp.to_numpy(),
        "call_state": [s for s, _ in call], "put_state": [s for s, _ in put],
        "call_bias": [b for _, b in call], "put_bias": [b for _, b in put],
    })


def buildup_signal(table: pd.DataFrame | None, spot: float,
                   window: float = 600.0) -> dict:
    """Aggregate the per-strike table into one buyer-vs-seller lean.

    ΔOI-weighted mass of bullish vs bearish strikes inside ``±window`` of spot →
    ``score`` in [-1, +1] (positive = buyers/bullish), ``bias`` (bullish/bearish/
    neutral), ``strength`` (|score|), plus the call/put **writing** mass (fresh
    supply = resistance/support forming) and the dominant fresh-OI strikes.

    Returns ``{"insufficient": True, ...}`` when there is nothing to read.
    """
    base = {
        "bias": "neutral", "score": 0.0, "strength": 0.0,
        "call_writing": 0.0, "put_writing": 0.0,
        "call_unwinding": 0.0, "put_unwinding": 0.0,
        "dominant_call_strike": None, "dominant_put_strike": None,
        "n": 0, "insufficient": True,
    }
    if table is None or table.empty:
        return base
    t = table[(table["strike"] - spot).abs() <= window]
    if t.empty:
        t = table
    if t.empty:
        return base

    d_call = pd.to_numeric(t["d_call_oi"], errors="coerce").fillna(0.0)
    d_put = pd.to_numeric(t["d_put_oi"], errors="coerce").fillna(0.0)
    cbias = pd.to_numeric(t["call_bias"], errors="coerce").fillna(0)
    pbias = pd.to_numeric(t["put_bias"], errors="coerce").fillna(0)

    # ΔOI-weighted mass on each side of the ledger.
    cw = d_call.abs()
    pw = d_put.abs()
    bull = float(cw[cbias > 0].sum() + pw[pbias > 0].sum())
    bear = float(cw[cbias < 0].sum() + pw[pbias < 0].sum())
    total = bull + bear
    score = 0.0 if total <= _EPS else (bull - bear) / total

    if score > 0.15:
        bias = "bullish"
    elif score < -0.15:
        bias = "bearish"
    else:
        bias = "neutral"

    call_writing = float(d_call[(t["call_state"] == SHORT_BUILDUP)].sum())
    put_writing = float(d_put[(t["put_state"] == SHORT_BUILDUP)].sum())
    call_unwind = float((-d_call[(t["call_state"] == LONG_UNWINDING)]).sum())
    put_unwind = float((-d_put[(t["put_state"] == LONG_UNWINDING)]).sum())

    dom_call = t.loc[d_call.idxmax(), "strike"] if (d_call > 0).any() else None
    dom_put = t.loc[d_put.idxmax(), "strike"] if (d_put > 0).any() else None

    return {
        "bias": bias,
        "score": round(score, 3),
        "strength": round(abs(score), 3),
        "call_writing": round(call_writing, 1),
        "put_writing": round(put_writing, 1),
        "call_unwinding": round(call_unwind, 1),
        "put_unwinding": round(put_unwind, 1),
        "dominant_call_strike": int(dom_call) if dom_call is not None else None,
        "dominant_put_strike": int(dom_put) if dom_put is not None else None,
        "n": int(len(t)),
        "insufficient": total <= _EPS,
    }


def earliest_snapshot(history: pd.DataFrame | None) -> pd.DataFrame | None:
    """The day-open baseline chain from a concatenated ``oi_store`` history frame.

    ``load_history`` returns every snapshot for the day stacked (rows carry a ``ts``
    column). The buildup baseline is the earliest-ts per-strike slice. Returns None
    when there's no history to anchor on (→ callers show ``insufficient``).
    """
    if history is None or getattr(history, "empty", True) or "ts" not in history.columns:
        return None
    ts_min = history["ts"].min()
    base = history[history["ts"] == ts_min]
    return base if not base.empty else None


def compute_buildup(prev_chain, cur_chain, spot, *, table_window: float = 1000.0,
                    signal_window: float = 600.0) -> dict:
    """Convenience: table + signal in one call. Returns ``{"signal", "table"}``
    (``table`` as a list of row dicts, ready for JSON / a display panel)."""
    tbl = buildup_table(prev_chain, cur_chain, spot, window=table_window)
    sig = buildup_signal(tbl, spot, window=signal_window)
    return {"signal": sig, "table": tbl.to_dict("records")}
