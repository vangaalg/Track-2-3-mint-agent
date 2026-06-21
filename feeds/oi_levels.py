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
