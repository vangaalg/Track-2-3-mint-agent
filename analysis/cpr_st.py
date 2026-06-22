"""CPR + Supertrend trend-rider — a directional mechanical strategy.

A second alert stream beside the journal 3-min trigger: on a narrow-CPR (trend/
expansion) day, ride the established Supertrend trend, entering on the first
5-EMA pullback that reclaims the fast EMA (see ``indicators.directional.
vote_cpr_supertrend`` for the exact mechanic). Vehicle = the same deep-ITM option;
levels = the shared structural model (``analysis.trade1.trade1_levels``).

OI is evaluated MANUALLY by the trader for this strategy, so — unlike Trade-1 —
no OI walls shape the levels and no Claude OI-boost is applied. The proposal is
propose-only and is *validated on the backtest rig* (``scoring.backtest
--strategy cpr_st``) before being trusted live.
"""

from __future__ import annotations

from analysis.proposal import TradeProposal
from analysis.trade1 import build_directional_proposal, DEFAULT_SIZE_LOTS


def propose_cpr_st(snapshot, cfg=None, size_lots: int = DEFAULT_SIZE_LOTS) -> TradeProposal:
    """Build a CPR-Supertrend proposal from a ``feeds.snapshot.Snapshot``."""
    from feeds.snapshot import chart_read_for
    from indicators.directional import cpr_st_mtf_config

    cfg = cfg or cpr_st_mtf_config()
    read = chart_read_for(snapshot.feats, cfg)
    return build_directional_proposal(
        instrument=snapshot.instrument, ts=snapshot.ts, spot=snapshot.spot,
        read=read, oi=snapshot.oi, macro=snapshot.macro, notes=snapshot.notes,
        trade_type="cpr_st", size_lots=size_lots, oi_levels=None,
    )
