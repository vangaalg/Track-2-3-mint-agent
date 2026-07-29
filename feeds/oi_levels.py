"""OI-wall support/resistance + the trader's extension bands (PURE).

The trader's method: the highest-OI strike is the wall — call wall = resistance,
put shelf = support (both already found by ``feeds.oi.summarise_chain``). Around each
wall sit extension bands at fixed point offsets; price breaches the strike out to a
band, then reverses back to the strike (the setup). For **NIFTY** the offsets are a
fixed ``[37, 72]``; other instruments scale those by price level (ATR-based later).

No new OI math — this only projects the bands off the walls summarise_chain found.
"""

from __future__ import annotations

NIFTY_REF_SPOT = 24000.0          # reference spot the 37/72 bands were calibrated at
NIFTY_BANDS = [37.0, 72.0]        # the trader's NIFTY-only extension offsets (points)


def scaled_offsets(spot, base: list[float] | None = None,
                   base_spot: float = NIFTY_REF_SPOT) -> list[float]:
    """Price-scale the NIFTY offsets to another instrument (≈ same %-of-price move).

    e.g. Bank Nifty near 52,000 scales 37/72 → ~80/156. Falls back to the base
    offsets if spot is missing. (An ATR-based variant can replace this later.)
    """
    base = list(base if base is not None else NIFTY_BANDS)
    if not spot or float(spot) <= 0:
        return base
    f = float(spot) / float(base_spot)
    return [round(o * f, 1) for o in base]


def wall_levels(summary: dict, offsets: list[float]) -> dict:
    """Project the extension bands off the OI walls in ``summarise_chain`` output.

    Returns the resistance/support strikes plus the band levels ABOVE the call wall
    (resistance + each offset) and BELOW the put shelf (support − each offset).
    """
    cw = (summary or {}).get("call_wall") or {}
    ps = (summary or {}).get("put_shelf") or {}
    res = cw.get("strike")
    sup = ps.get("strike")
    return {
        "resistance_strike": res,
        "support_strike": sup,
        "resistance_ext": [round(res + o, 2) for o in offsets] if res is not None else [],
        "support_ext": [round(sup - o, 2) for o in offsets] if sup is not None else [],
    }


def reversal_levels(summary: dict, offsets: list[float], buildup: dict | None = None) -> dict:
    """APPROXIMATE bullish/bearish reversal levels off the OI walls + extension bands.

    LTPCalculator surfaces "EOS" (a bullish decision level) and "EOR" (a bearish one)
    from *proprietary* Reversal-Price / COA / 9:20 models we cannot reproduce. This is a
    transparent stand-in built only from data we already have: the put shelf (support)
    anchors the **bull reversal** level (bounce zone) and the call wall (resistance)
    anchors the **bear reversal** level (rejection zone); the first extension band gives
    the "+1" overshoot LTPCalc marks beyond each. When a ``buildup`` signal is present,
    a *defended* wall (fresh writing on that side) is treated as the firmer level (noted
    in ``defended``). ``source="approx"`` flags that this is NOT LTPCalc's formula.
    """
    cw = (summary or {}).get("call_wall") or {}
    ps = (summary or {}).get("put_shelf") or {}
    res = cw.get("strike")     # resistance = call wall
    sup = ps.get("strike")     # support    = put shelf
    o1 = offsets[0] if offsets else 0.0
    bu = buildup or {}
    defended = None
    if bu.get("put_writing", 0) > abs(bu.get("call_writing", 0)):
        defended = "support"           # puts being written → support firmer (bullish)
    elif bu.get("call_writing", 0) > abs(bu.get("put_writing", 0)):
        defended = "resistance"        # calls being written → resistance firmer (bearish)
    return {
        "source": "approx",            # NOT LTPCalculator's proprietary EOS/EOR/COA math
        "bull_reversal": sup,                                            # ≈ EOS (support bounce)
        "bull_reversal_ext": round(sup - o1, 2) if sup is not None else None,   # ≈ EOS-1 overshoot
        "bear_reversal": res,                                           # ≈ EOR (resistance reject)
        "bear_reversal_ext": round(res + o1, 2) if res is not None else None,   # ≈ EOR+1 overshoot
        "defended": defended,
    }
