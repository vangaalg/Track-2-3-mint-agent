"""Strike-selection agent (LIVE only).

The trader trades a deep-enough ITM option (~0.8-1.0 delta) but does NOT want to
overpay theta: among ITM strikes within ~1000 points of spot, take the one
**nearest to money whose time-value (extrinsic = LTP - intrinsic) is low** — the
least-deep strike (least premium / capital) that still has little to decay. Only
step deeper when the nearer strike's extrinsic is too rich.

Rule (confirmed with the trader): nearest-to-money ITM strike with
``extrinsic <= max_extrinsic`` (≈25 pts); if none within ``max_itm`` qualifies,
fall back to the lowest-extrinsic strike. Example: spot 24000, up-trend —
23500 CE @510 (extrinsic 10) is taken; at @550 (extrinsic 50) it steps deeper.

Operates on the per-strike table from ``feeds.oi.chain_table`` (which already
computes ``call_extrinsic`` / ``put_extrinsic``). LIVE only: needs the live
per-strike chain LTPs, so this runs in the web layer, not in ``propose_trade1``.
"""

from __future__ import annotations

import pandas as pd


def select_strike(
    table: pd.DataFrame,
    spot: float,
    direction: str,
    max_itm: float = 1000.0,
    max_extrinsic: float = 25.0,
) -> dict | None:
    """Pick the ITM vehicle strike for a long (CE) / short (PE) trade.

    Args:
        table: the ``chain_table`` frame — needs ``strike``, ``call_ltp`` /
            ``put_ltp`` and ``call_extrinsic`` / ``put_extrinsic``.
        spot: current spot.
        direction: ``"long"`` (buy CE) or ``"short"`` (buy PE).
        max_itm: how deep ITM we'll go (points from spot).
        max_extrinsic: max time-value (theta proxy) we'll pay before stepping deeper.

    Returns:
        ``{"strike", "right", "ltp", "extrinsic", "intrinsic"}`` or ``None`` when
        no ITM strike with a quoted LTP exists within ``max_itm``.
    """
    if direction not in ("long", "short") or table is None or table.empty:
        return None
    long = direction == "long"
    right = "CE" if long else "PE"
    ltp_col, ext_col = ("call_ltp", "call_extrinsic") if long else ("put_ltp", "put_extrinsic")
    if ltp_col not in table.columns or ext_col not in table.columns:
        return None

    if long:   # CE is ITM below spot; nearest-to-money first = highest strike
        cand = table[(table["strike"] < spot) & (spot - table["strike"] <= max_itm)]
        cand = cand.sort_values("strike", ascending=False)
    else:      # PE is ITM above spot; nearest-to-money first = lowest strike
        cand = table[(table["strike"] > spot) & (table["strike"] - spot <= max_itm)]
        cand = cand.sort_values("strike", ascending=True)

    cand = cand[pd.to_numeric(cand[ltp_col], errors="coerce").notna()]
    if cand.empty:
        return None

    ext = pd.to_numeric(cand[ext_col], errors="coerce")
    ok = cand[ext <= max_extrinsic]
    # nearest-to-money that clears the theta bar, else the lowest-extrinsic strike
    row = ok.iloc[0] if not ok.empty else cand.iloc[ext.reset_index(drop=True).idxmin()]

    ltp = float(row[ltp_col])
    extrinsic = float(row[ext_col])
    return {
        "strike": int(row["strike"]),
        "right": right,
        "ltp": round(ltp, 2),
        "extrinsic": round(extrinsic, 2),
        "intrinsic": round(ltp - extrinsic, 2),
    }
