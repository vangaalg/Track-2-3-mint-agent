"""OI-buildup watchlist scan (PURE) — flag the scripts with a CLEAR lean.

Reads the latest recorded OI summary row per instrument (the recorder / cockpit already
persist bias/score/writing + walls + the dominant ΔOI strikes to ``oi_summary``) and
turns each into a compact watchlist card: net lean, whether it's CLEAR (strong), the
Support/Resistance + EOS/EOR levels, and the strike where OI is SHIFTING most. No Breeze
pulls here — it's a store read, so it scales across NIFTY / Bank Nifty / Fin Nifty + the
day's most-liquid F&O stocks cheaply. Clear leans sort first so the trader focuses there.
"""

from __future__ import annotations

STRONG = 0.4      # |score| for a CLEAR lean (mirrors analysis.trade1.BUILDUP_STRONG + the UI)


def _clean(v):
    """NaN → None (summary rows written before buildup existed carry NULL→NaN columns;
    FastAPI encodes JSON with allow_nan=False, so a stray NaN 500s the endpoint)."""
    try:
        if v is None or (isinstance(v, float) and v != v):
            return None
    except Exception:
        return None
    return v


def build_card(symbol: str, row: dict | None, strong: float = STRONG) -> dict:
    """One watchlist card from a summary row (``oi_summary_store`` row dict) or None.

    ``clear`` = the ΔOI lean is strong (|score| >= ``strong``) and directional. EOS≈support
    (put shelf, bullish bounce), EOR≈resistance (call wall, bearish reject); the *_ext are
    the overshoot bands. ``shift_strike`` is the focus strike — where fresh OI concentrates
    for the leaning side (puts written → support; calls written → resistance)."""
    if not row:
        return {"symbol": symbol, "has_data": False, "bias": "neutral", "clear": False}
    g = lambda k: _clean(row.get(k))                 # NaN-safe (NULL buildup cols → NaN → 500)
    score = g("buildup_score")
    strength = abs(score) if score is not None else 0.0
    bias = g("buildup_bias") or "neutral"
    clear = bias in ("bullish", "bearish") and strength >= strong
    shift = g("buildup_put_strike") if bias == "bullish" else (
        g("buildup_call_strike") if bias == "bearish" else None)
    return {
        "symbol": symbol, "has_data": True,
        "ts": row.get("ts"), "spot": g("spot"),
        "bias": bias, "score": score, "strength": round(strength, 3), "clear": clear,
        "resistance": g("call_wall_strike"), "support": g("put_shelf_strike"),
        "eor": g("call_wall_strike"), "eor_ext": g("res_ext1"),      # ≈ EOR / EOR+1
        "eos": g("put_shelf_strike"), "eos_ext": g("sup_ext1"),      # ≈ EOS / EOS-1
        "shifting_call_strike": g("buildup_call_strike"),
        "shifting_put_strike": g("buildup_put_strike"),
        "shift_strike": shift,                                       # the focus strike for the lean
        "call_writing": g("call_writing"), "put_writing": g("put_writing"),
        "pcr": g("pcr"),
    }


def _last_row(df) -> dict | None:
    """The newest summary row as a plain dict (``load_summary`` returns a ts-indexed frame)."""
    if df is None or getattr(df, "empty", True):
        return None
    r = df.iloc[-1].to_dict()
    r.setdefault("ts", str(df.index[-1]))
    return r


def scan(symbols, load_summary_fn, strong: float = STRONG) -> list[dict]:
    """Build a card per symbol (``load_summary_fn(symbol)`` -> summary frame or None),
    CLEAR leans first (by strength), then symbols with data, then the rest — so the
    scripts worth focusing on rise to the top."""
    cards = [build_card(s, _last_row(load_summary_fn(s)), strong) for s in symbols]
    cards.sort(key=lambda c: (not c["clear"], not c["has_data"], -(c.get("strength") or 0.0)))
    return cards
