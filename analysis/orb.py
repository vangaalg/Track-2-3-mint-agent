"""Opening-Range Breakout + VWAP — a directional mechanical strategy.

A third alert stream: mark the first-15-minute high/low; go long on a 3-min close
above the OR-high (short below the OR-low) only when price is on the right side of
VWAP and the 45-EMA (see ``indicators.directional.vote_orb_vwap``). One shot per
side per day. Vehicle = the deep-ITM option; levels = the shared structural model.

Like the CPR-Supertrend strategy, OI is the trader's MANUAL cross-check here, so no
OI walls shape the levels and no Claude OI-boost is applied. Propose-only; validated
on ``scoring.backtest --strategy orb`` before being trusted live.
"""

from __future__ import annotations

from analysis.proposal import TradeProposal
from analysis.trade1 import build_directional_proposal, DEFAULT_SIZE_LOTS


def propose_orb(snapshot, cfg=None, size_lots: int = DEFAULT_SIZE_LOTS) -> TradeProposal:
    """Build an Opening-Range-Breakout proposal from a ``feeds.snapshot.Snapshot``."""
    from feeds.snapshot import chart_read_for
    from indicators.directional import orb_mtf_config

    cfg = cfg or orb_mtf_config()
    read = chart_read_for(snapshot.feats, cfg)
    return build_directional_proposal(
        instrument=snapshot.instrument, ts=snapshot.ts, spot=snapshot.spot,
        read=read, oi=snapshot.oi, macro=snapshot.macro, notes=snapshot.notes,
        trade_type="orb", size_lots=size_lots, oi_levels=None,
    )
