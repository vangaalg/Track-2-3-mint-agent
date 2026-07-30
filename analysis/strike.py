"""Strike-selection agent (LIVE only).

The trader trades a deep-enough ITM option (~0.8-1.0 delta) but does NOT want to
overpay theta: among ITM strikes within ~1000 points of spot, take the one
**nearest to money whose time-value is a small fraction of its premium** — the
least-deep strike (least premium / capital) that is still mostly intrinsic (little
to decay). Only step deeper when the nearer strike's time value is too rich.

Rule (confirmed with the trader): "value for money" = nearest-to-money ITM strike
whose **time value (extrinsic = LTP − intrinsic) ≤ ``max_pct`` of the premium**
(default 10%). The %-of-premium test scales with the option price, so it lands near
the trader's deep-ITM ₹-signature vehicle instead of walking needlessly deep the way
an absolute points cutoff does. If nothing within ``max_itm`` qualifies, fall back to
the lowest time-value strikes. Pass ``max_extrinsic`` (points) to use the old
ABSOLUTE cutoff instead (back-compat).

``rank_strikes`` returns the top-N candidates (best first) so the trader can pick;
``select_strike`` returns just the best. Operates on the per-strike table from
``feeds.oi.chain_table`` (which already computes ``call_extrinsic`` /
``put_extrinsic``). LIVE only: needs the live per-strike chain LTPs, so this runs in
the web layer, not in ``propose_trade1``.
"""

from __future__ import annotations

import pandas as pd

DEFAULT_MAX_PCT = 0.10          # time value must be <= this fraction of premium (10%)


def _buildup_state_for(buildup_table, strike: int, right: str) -> str | None:
    """Look up the buildup state (long_buildup/writing/…) for a strike + side."""
    if buildup_table is None:
        return None
    try:
        if getattr(buildup_table, "empty", True):
            return None
        row = buildup_table[buildup_table["strike"] == strike]
        if row.empty:
            return None
        col = "call_state" if right == "CE" else "put_state"
        val = row.iloc[0].get(col)
        return None if val in (None, "flat", "unknown") else str(val)
    except Exception:
        return None


def _extrinsic_pct(extrinsic: float, ltp: float) -> float:
    return (extrinsic / ltp) if ltp and ltp > 0 else float("inf")


def rank_strikes(
    table: pd.DataFrame,
    spot: float,
    direction: str,
    max_itm: float = 1000.0,
    max_pct: float = DEFAULT_MAX_PCT,
    max_extrinsic: float | None = None,
    top_n: int = 3,
    buildup_table=None,
) -> list[dict]:
    """Top value-for-money ITM vehicle strikes for a long (CE) / short (PE), best first.

    The best pick is the CLOSEST-to-money ITM strike whose time value clears the bar
    (``extrinsic <= max_pct*ltp``, or ``<= max_extrinsic`` points when that's given);
    the rest of the ladder is the next deeper qualifying strikes (progressively less
    theta / more capital). When nothing qualifies, the lowest-time-value strikes.

    Each candidate: ``{strike, right, ltp, extrinsic, extrinsic_pct, intrinsic,
    buildup_state}``. Empty list when there's no ITM strike with a quoted LTP.
    """
    if direction not in ("long", "short") or table is None or table.empty:
        return []
    long = direction == "long"
    right = "CE" if long else "PE"
    ltp_col, ext_col = ("call_ltp", "call_extrinsic") if long else ("put_ltp", "put_extrinsic")
    if ltp_col not in table.columns or ext_col not in table.columns:
        return []

    if long:   # CE is ITM below spot; nearest-to-money first = highest strike
        cand = table[(table["strike"] < spot) & (spot - table["strike"] <= max_itm)]
        cand = cand.sort_values("strike", ascending=False)
    else:      # PE is ITM above spot; nearest-to-money first = lowest strike
        cand = table[(table["strike"] > spot) & (table["strike"] - spot <= max_itm)]
        cand = cand.sort_values("strike", ascending=True)

    cand = cand[pd.to_numeric(cand[ltp_col], errors="coerce").notna()].reset_index(drop=True)
    if cand.empty:
        return []

    rows = []
    for _, r in cand.iterrows():
        ltp, ext = float(r[ltp_col]), float(r[ext_col])
        rows.append({
            "strike": int(r["strike"]), "right": right,
            "ltp": round(ltp, 2), "extrinsic": round(ext, 2),
            "extrinsic_pct": round(_extrinsic_pct(ext, ltp), 4),
            "intrinsic": round(ltp - ext, 2),
            "buildup_state": _buildup_state_for(buildup_table, int(r["strike"]), right),
        })

    if max_extrinsic is not None:                       # back-compat absolute-points cutoff
        qual = [x for x in rows if x["extrinsic"] <= max_extrinsic]
    else:
        qual = [x for x in rows if x["extrinsic_pct"] <= max_pct]
    ranked = qual if qual else sorted(rows, key=lambda x: x["extrinsic_pct"])
    return ranked[:max(1, top_n)]


def select_strike(
    table: pd.DataFrame,
    spot: float,
    direction: str,
    max_itm: float = 1000.0,
    max_pct: float = DEFAULT_MAX_PCT,
    max_extrinsic: float | None = None,
    buildup_table=None,
) -> dict | None:
    """The single best value-for-money ITM vehicle (``rank_strikes[0]``), or None."""
    picks = rank_strikes(table, spot, direction, max_itm=max_itm, max_pct=max_pct,
                         max_extrinsic=max_extrinsic, buildup_table=buildup_table, top_n=1)
    return picks[0] if picks else None
