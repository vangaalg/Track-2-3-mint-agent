"""3:15 PM CAS (Closing Auction Session) setup — Excess-Premium engine (PURE).

Mechanism (live since Aug 3, 2026): F&O stocks stop cash trading at 15:15 IST and
enter a ~20-min closing auction; Nifty SPOT freezes at its 15:15 level until the
auction close prints (~15:35), while futures & options keep trading to 15:40. At
15:15 all flow concentrates into derivatives → a short futures burst (60–120 s).
The auction close lands near futures-minus-carry (verified Aug 5 2026: spot froze
24,570, futures ~24,648, carry ~25, CAS printed 24,624.65).

Setup: at 15:13 compute ``EP = Futures − Spot − FairCarry``.
  EP ≥ +min_ep → expect an upward futures burst at 15:15 → buy ITM CE (Δ ≥ 0.65)
  at ~15:14, exit by 15:16:30, HARD FLAT by 15:18.
  EP ≤ −min_ep → mirror with ITM PE.  |EP| < min_ep → NO TRADE.

Status discipline (baked into the payload, not hidden): 10-session PAPER-LOG phase;
the burst coefficient k (fraction of EP that converts into the futures move) is being
calibrated (initial k ≈ 0.5 from Aug 5: EP ~55 → burst ~25). One lot only when live.
The edge is expected to DECAY within weeks as arbs adapt; the Sept 7 pre-open
restructure requires re-validation from scratch.
"""

from __future__ import annotations

import pandas as pd

FAIR_CARRY_DEFAULT = 27.0        # ≈25–30 pts for the Aug-2026 monthly; env/knob-tunable
MIN_EP_DEFAULT = 15.0            # |EP| below this → no trade
PAPER_SESSIONS = 10              # calibration phase length (sessions)
MAX_BASIS_PCT = 3.0              # index futures trade within ~1% of spot; beyond this the spot leg is bad

# The decision-window timeline (IST). Times as "HH:MM:SS" for the checklist.
TIMELINE = [
    ("15:10:00", "warm-up — watch EP stabilise"),
    ("15:13:00", "DECIDE: compute EP, pick CE/PE/no-trade"),
    ("15:14:00", "ENTER the ITM option (Δ ≥ 0.65), one lot (paper for now)"),
    ("15:15:00", "cash freezes — burst window opens"),
    ("15:16:30", "EXIT target — take the burst"),
    ("15:18:00", "HARD FLAT — no exceptions"),
    ("15:35:00", "CAS close prints (≈ futures − carry) — log the burst"),
]


def excess_premium(futures: float | None, spot: float | None,
                   fair_carry: float = FAIR_CARRY_DEFAULT) -> float | None:
    """``EP = Futures − Spot − FairCarry`` (None when either leg is missing)."""
    if futures is None or spot is None:
        return None
    return round(float(futures) - float(spot) - float(fair_carry), 2)


def spot_looks_valid(spot, futures, max_basis_pct: float = MAX_BASIS_PCT) -> bool:
    """True when spot is a plausible cash level for these futures. An index future trades
    within a fraction of a percent of spot (basis = carry), so a gap beyond ``max_basis_pct``
    means the spot quote is wrong — a bad tick or a stale snapshot (the real 6-Aug bug: a
    NIFTY future at 24712 with spot read as 14018 = 43% off = impossible). Guards the EP so a
    garbage spot never produces a garbage "BUY ITM" signal. With no futures to compare against
    a lone spot is accepted (nothing to cross-check)."""
    if spot is None:
        return False
    if futures is None:
        return True
    try:
        s, f = float(spot), float(futures)
        return s > 0 and f > 0 and abs(f - s) <= (max_basis_pct / 100.0) * f
    except Exception:
        return False


def cas_signal(ep: float | None, min_ep: float = MIN_EP_DEFAULT) -> dict:
    """The trade decision from EP: side CE/PE/None + a plain-language line."""
    if ep is None:
        return {"side": None, "action": "no data — futures or spot quote missing", "ep": None}
    if ep >= min_ep:
        return {"side": "CE", "ep": ep,
                "action": f"EP +{ep:.1f} ≥ {min_ep:.0f} → BUY ITM CE (Δ≥0.65) at ~15:14, "
                          "exit 15:16:30, hard flat 15:18"}
    if ep <= -min_ep:
        return {"side": "PE", "ep": ep,
                "action": f"EP {ep:.1f} ≤ -{min_ep:.0f} → BUY ITM PE (Δ≥0.65) at ~15:14, "
                          "exit 15:16:30, hard flat 15:18"}
    return {"side": None, "ep": ep,
            "action": f"|EP| {abs(ep):.1f} < {min_ep:.0f} → NO TRADE"}


def phase(now) -> str:
    """Where we are in the CAS timeline: pre / decide / enter / burst / flatten / auction / done."""
    t = pd.Timestamp(now)
    hm = t.strftime("%H:%M:%S")
    if hm < "15:10:00":
        return "pre"
    if hm < "15:13:00":
        return "warmup"
    if hm < "15:14:00":
        return "decide"
    if hm < "15:15:00":
        return "enter"
    if hm < "15:16:30":
        return "burst"
    if hm < "15:18:00":
        return "flatten"
    if hm < "15:40:00":
        return "auction"
    return "done"


def estimate_k(rows: list[dict]) -> float | None:
    """Burst coefficient k from the paper log: least squares through the origin over
    (ep, burst_pts) pairs — k = Σ(ep·burst)/Σ(ep²). None until 2+ usable sessions."""
    pairs = [(r.get("ep"), r.get("burst_pts")) for r in rows
             if r.get("ep") not in (None, 0) and r.get("burst_pts") is not None]
    if len(pairs) < 2:
        return None
    num = sum(e * b for e, b in pairs)
    den = sum(e * e for e, _ in pairs)
    return round(num / den, 3) if den else None


def cas_card(spot, futures, now, fair_carry: float = FAIR_CARRY_DEFAULT,
             min_ep: float = MIN_EP_DEFAULT, log_rows: list[dict] | None = None) -> dict:
    """Everything the 15:13 decision needs, in one payload."""
    # A spot that can't be right for these futures (bad tick / stale snapshot) must NOT drive a
    # trade — suppress the EP + signal and say so, rather than print "BUY ITM CE EP +10667".
    suspect = spot is not None and futures is not None and not spot_looks_valid(spot, futures)
    ep = None if suspect else excess_premium(futures, spot, fair_carry)
    sig = cas_signal(ep, min_ep)
    if suspect:
        sig = {"side": None, "ep": None,
               "action": (f"⚠ spot feed looks wrong (futures {float(futures):.0f} vs "
                          f"spot {float(spot):.0f}) — EP suppressed; check the NIFTY quote")}
    rows = log_rows or []
    k = estimate_k(rows)
    expected = round(ep * k, 1) if (ep is not None and k is not None) else None
    return {
        "spot": spot, "futures": futures, "fair_carry": fair_carry, "spot_suspect": suspect,
        "ep": ep, "min_ep": min_ep, "signal": sig, "phase": phase(now),
        "timeline": TIMELINE,
        "k": k, "expected_burst_pts": expected,
        "sessions_logged": len(rows), "paper_sessions": PAPER_SESSIONS,
        "paper_phase": len(rows) < PAPER_SESSIONS,
        "note": ("PAPER-LOG phase: log, don't trade. One lot only when live. Edge decays as "
                 "arbs adapt; re-validate after the Sept-7 pre-open restructure."),
    }
