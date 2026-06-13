"""Directional-output resolver — the single ``long / short / flat`` call.

The chart stack's indicators won't always agree (EMA up, RSI overbought,
Bollinger mid-band). Collapsing them into ONE call is the crux of the read.
Per the build instruction we do **not** hardcode one method — the resolver is a
config switch between:

  * ``confluence``   — N-of-M indicator votes must agree, else flat.
  * ``hierarchical`` — one primary indicator decides direction; the others act
                       as filters/veto.

This lets Stage-1 scoring empirically test which wins **per instrument**. The
design is deliberately flexible enough to express
*hierarchical-with-confluence-confirmation* (primary decides, then a confluence
gate on the remaining voters must confirm) via ``confirm_min`` on the
hierarchical method.

See ``DIRECTIONAL_SPEC.md`` in this directory for the full spec.

Vote convention everywhere: ``+1`` long, ``-1`` short, ``0`` flat/abstain.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

LONG, SHORT, FLAT = "long", "short", "flat"


# --------------------------------------------------------------------------- #
# Per-indicator voters
# --------------------------------------------------------------------------- #
# Each voter maps the indicator columns produced by engine.compute_indicators
# into a vote Series in {-1, 0, +1}. Interpretations are configurable because
# the "right" reading (e.g. RSI as momentum vs mean-reversion) is itself an
# empirical question Stage 1 answers. Keep these pure and column-driven.

def vote_ema(df: pd.DataFrame, fast: int = 9, slow: int = 21) -> pd.Series:
    """Fast EMA above slow EMA -> long; below -> short."""
    f, s = df[f"ema_{fast}"], df[f"ema_{slow}"]
    v = pd.Series(0, index=df.index, dtype="int8")
    v[f > s] = 1
    v[f < s] = -1
    return v.rename("vote_ema")


def vote_macd(df: pd.DataFrame) -> pd.Series:
    """MACD histogram > 0 -> long; < 0 -> short."""
    v = pd.Series(0, index=df.index, dtype="int8")
    v[df["macd_hist"] > 0] = 1
    v[df["macd_hist"] < 0] = -1
    return v.rename("vote_macd")


def vote_rsi(df: pd.DataFrame, period: int = 14, mode: str = "momentum") -> pd.Series:
    """RSI vote.

    ``mode="momentum"``: RSI > 50 long, < 50 short.
    ``mode="reversion"``: RSI < 30 long (oversold), > 70 short (overbought).
    Which mode is correct is per-instrument — let Stage 1 decide.
    """
    r = df[f"rsi_{period}"]
    v = pd.Series(0, index=df.index, dtype="int8")
    if mode == "momentum":
        v[r > 50] = 1
        v[r < 50] = -1
    elif mode == "reversion":
        v[r < 30] = 1
        v[r > 70] = -1
    else:
        raise ValueError(f"unknown rsi mode: {mode!r}")
    return v.rename("vote_rsi")


def vote_bollinger(df: pd.DataFrame, mode: str = "reversion") -> pd.Series:
    """Bollinger vote off %B.

    ``mode="reversion"``: %B < 0 long (below lower band), %B > 1 short.
    ``mode="breakout"``: %B > 1 long, %B < 0 short.
    """
    b = df["bb_pctb"]
    v = pd.Series(0, index=df.index, dtype="int8")
    if mode == "reversion":
        v[b < 0] = 1
        v[b > 1] = -1
    elif mode == "breakout":
        v[b > 1] = 1
        v[b < 0] = -1
    else:
        raise ValueError(f"unknown bollinger mode: {mode!r}")
    return v.rename("vote_bb")


def vote_three_min(df: pd.DataFrame) -> pd.Series:
    """Aggregate the three 3-min strategy component signals into one vote.

    Sums the component signals and takes the sign; ties -> flat. The components
    themselves are journal-derived stubs in engine.py.
    """
    combo = (
        df["sig_ema_meanrev"].astype(int)
        + df["sig_bb_vrl"].astype(int)
        + df["sig_sma_pullback"].astype(int)
    )
    return np.sign(combo).astype("int8").rename("vote_3min")


# Registry: name -> callable(df) -> vote Series. The config references voters by
# name so the indicator set is fully data-driven.
VOTERS = {
    "ema": vote_ema,
    "macd": vote_macd,
    "rsi": vote_rsi,
    "bollinger": vote_bollinger,
    "three_min": vote_three_min,
}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class DirectionalConfig:
    """Resolver configuration — THE config flag.

    Attributes:
        method: ``"confluence"`` or ``"hierarchical"``.
        voters: ordered list of voter names (keys of ``VOTERS``) to include.
        voter_kwargs: optional per-voter keyword overrides, keyed by voter name
            (e.g. ``{"rsi": {"mode": "reversion"}}``).
        min_agree: [confluence] minimum net agreement required to take a side.
            With M voters, a call needs at least ``min_agree`` more votes on one
            side than the other; otherwise flat. (e.g. 4-of-6.)
        primary: [hierarchical] voter name that decides direction.
        confirm_min: [hierarchical] how many of the remaining voters must AGREE
            with the primary to confirm. ``0`` = pure hierarchical (others only
            veto). ``>0`` = hierarchical-with-confluence-confirmation.
        veto: [hierarchical] if True, any remaining voter pointing the opposite
            way to the primary forces flat.
    """

    method: str = "confluence"
    voters: list[str] = field(
        default_factory=lambda: ["ema", "macd", "rsi", "bollinger", "three_min"]
    )
    voter_kwargs: dict[str, dict] = field(default_factory=dict)

    # confluence
    min_agree: int = 3

    # hierarchical
    primary: str = "ema"
    confirm_min: int = 0
    veto: bool = True

    def validate(self) -> None:
        if self.method not in ("confluence", "hierarchical"):
            raise ValueError(f"unknown method: {self.method!r}")
        unknown = [v for v in self.voters if v not in VOTERS]
        if unknown:
            raise ValueError(f"unknown voter(s): {unknown}. Known: {list(VOTERS)}")
        if self.method == "hierarchical" and self.primary not in self.voters:
            raise ValueError("hierarchical primary must be one of the voters")


# --------------------------------------------------------------------------- #
# Vote assembly
# --------------------------------------------------------------------------- #
def _collect_votes(df: pd.DataFrame, cfg: DirectionalConfig) -> pd.DataFrame:
    """Build a (bars x voters) integer vote matrix."""
    cols = {}
    for name in cfg.voters:
        kwargs = cfg.voter_kwargs.get(name, {})
        cols[name] = VOTERS[name](df, **kwargs).astype(int)
    return pd.DataFrame(cols, index=df.index)


# --------------------------------------------------------------------------- #
# Resolvers
# --------------------------------------------------------------------------- #
def _resolve_confluence(votes: pd.DataFrame, cfg: DirectionalConfig) -> pd.Series:
    longs = (votes > 0).sum(axis=1)
    shorts = (votes < 0).sum(axis=1)
    net = longs - shorts
    out = pd.Series(FLAT, index=votes.index, dtype=object)
    out[net >= cfg.min_agree] = LONG
    out[net <= -cfg.min_agree] = SHORT
    return out


def _resolve_hierarchical(votes: pd.DataFrame, cfg: DirectionalConfig) -> pd.Series:
    primary = votes[cfg.primary]
    others = votes.drop(columns=[cfg.primary])

    direction = np.sign(primary)  # -1/0/+1, the primary's call

    # confirmation: how many others agree with the primary's sign
    agree = (np.sign(others).eq(direction, axis=0) & (direction != 0)).sum(axis=1)
    # veto: any other points strictly opposite the primary
    opposite = (np.sign(others).eq(-direction, axis=0) & (direction != 0)).sum(axis=1)

    take = direction != 0
    if cfg.confirm_min > 0:
        take &= agree >= cfg.confirm_min
    if cfg.veto:
        take &= opposite == 0

    out = pd.Series(FLAT, index=votes.index, dtype=object)
    out[take & (direction > 0)] = LONG
    out[take & (direction < 0)] = SHORT
    return out


def resolve_direction(
    df: pd.DataFrame,
    cfg: DirectionalConfig | None = None,
    return_votes: bool = False,
):
    """Resolve indicator columns into a per-bar ``long / short / flat`` Series.

    Args:
        df: frame already passed through ``engine.compute_indicators``.
        cfg: resolver config (defaults to confluence, all voters, min_agree=3).
        return_votes: if True, also return the raw vote matrix (for debugging /
            scoring diagnostics).

    Returns:
        ``Series[str]`` of calls, or ``(calls, votes_df)`` if ``return_votes``.
    """
    cfg = cfg or DirectionalConfig()
    cfg.validate()
    votes = _collect_votes(df, cfg)

    if cfg.method == "confluence":
        calls = _resolve_confluence(votes, cfg)
    else:
        calls = _resolve_hierarchical(votes, cfg)
    calls = calls.rename("direction")

    return (calls, votes) if return_votes else calls
