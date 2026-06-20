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

from indicators.timeframes import align_to_base

LONG, SHORT, FLAT = "long", "short", "flat"
_CALL_TO_SIGN = {LONG: 1, SHORT: -1, FLAT: 0}


# --------------------------------------------------------------------------- #
# Per-indicator voters
# --------------------------------------------------------------------------- #
# Each voter maps the indicator columns produced by engine.compute_indicators
# into a vote Series in {-1, 0, +1}. Interpretations are configurable because
# the "right" reading (e.g. RSI as momentum vs mean-reversion) is itself an
# empirical question Stage 1 answers. Keep these pure and column-driven.

def vote_ema(df: pd.DataFrame, fast: int = 5, slow: int = 45) -> pd.Series:
    """Fast EMA above slow EMA -> long; below -> short."""
    f, s = df[f"ema_{fast}"], df[f"ema_{slow}"]
    v = pd.Series(0, index=df.index, dtype="int8")
    v[f > s] = 1
    v[f < s] = -1
    return v.rename("vote_ema")


def vote_ema_stack(
    df: pd.DataFrame, periods: tuple[int, ...] = (5, 45, 100, 200)
) -> pd.Series:
    """Full EMA-ribbon alignment.

    EMAs stacked strictly fastest>...>slowest -> long (trend up); strictly
    fastest<...<slowest -> short; anything tangled -> flat. This is the trader's
    5/45/100/200 ribbon read.
    """
    cols = [df[f"ema_{p}"] for p in periods]
    up = pd.Series(True, index=df.index)
    down = pd.Series(True, index=df.index)
    for faster, slower in zip(cols, cols[1:]):
        up &= faster > slower
        down &= faster < slower
    v = pd.Series(0, index=df.index, dtype="int8")
    v[up] = 1
    v[down] = -1
    return v.rename("vote_ema_stack")


def vote_supertrend(df: pd.DataFrame) -> pd.Series:
    """Supertrend direction: +1 uptrend long, -1 downtrend short."""
    return df["st_dir"].astype("int8").rename("vote_supertrend")


def vote_regime_45(df: pd.DataFrame) -> pd.Series:
    """The journal's MASTER regime filter: price vs the 45-EMA.

    "Danger while spot > 45-EMA; need closes below it." Close above the 45-EMA
    -> long-regime (+1), below -> short-regime (-1). Most meaningful on the bias
    timeframes (daily/weekly), where the MTF htf_bias_trigger consumes it.
    """
    e = df["ema_45"]
    v = pd.Series(0, index=df.index, dtype="int8")
    v[df["close"] > e] = 1
    v[df["close"] < e] = -1
    return v.rename("vote_regime_45")


def vote_ema5_trigger(df: pd.DataFrame) -> pd.Series:
    """The journal's 3-min entry trigger: close holding above/below the 5-EMA.

    Reads the engine's ``sig_ema5_trigger`` column. Pair with ``confirm_2_close``
    at the resolver layer for the journal's "2 closes + volume" confirmation.
    """
    return df["sig_ema5_trigger"].astype("int8").rename("vote_ema5_trigger")


def vote_cpr(df: pd.DataFrame) -> pd.Series:
    """CPR position: close above the top central line -> long, below the bottom
    central line -> short, inside the range -> flat.

    ``engine.cpr`` broadcasts the classic **daily** CPR onto every bar, so this
    voter is meaningful on the 3-min trigger as well as the higher TFs (close vs
    the day's central range — the level the trader's chart overlays).
    """
    close, tc, bc = df["close"], df["cpr_tc"], df["cpr_bc"]
    v = pd.Series(0, index=df.index, dtype="int8")
    v[close > tc] = 1
    v[close < bc] = -1
    return v.rename("vote_cpr")


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
    """Aggregate the journal's 3-min strategy trio into one vote.

    Sums the three journal-faithful components — EMA-5 trigger + Bollinger
    squeeze/VRL recovery + 45-EMA pullback continuation — and takes the sign;
    ties -> flat. (The ``sig_ema_meanrev`` experiment is deliberately excluded;
    the trader trends, he does not fade.)
    """
    # bb_vrl / sma_pullback are NaN during warm-up (~50 / ~200 bars) -> treat as 0
    combo = (
        df["sig_ema5_trigger"].fillna(0).astype(int)
        + df["sig_bb_vrl"].fillna(0).astype(int)
        + df["sig_sma_pullback"].fillna(0).astype(int)
    )
    return np.sign(combo).astype("int8").rename("vote_3min")


def vote_bb_reversal(df: pd.DataFrame) -> pd.Series:
    """SEPARATE squeeze-fade strategy: a Bollinger breach -> revert, EMA-5 confirmed.

    NOTE: this is NOT the trader's main 3-min entry (that is ``vote_breakout_pullback``
    below — a breakout CONTINUATION). This one fades a poke outside the band after a
    volatility *squeeze* (coil) re-expands; kept as a distinct signal to study, off by
    default.

    A squeeze-gated Bollinger reversal (``sig_bb_vrl``) whose close lands on the
    matching side of the EMA-5 (``sig_ema5_trigger`` agrees) ARMS a direction; it is
    HELD while the EMA-5 close stays on that side, and cleared when the EMA-5 flips.
    The latch turns the one-bar event into a held direction so the resolver's
    ``confirm_2_close`` and the flip-detection fire exactly one trigger per reversal.
    """
    ema5 = df["sig_ema5_trigger"].fillna(0).to_numpy()
    bbv = df["sig_bb_vrl"].fillna(0).to_numpy()
    out = np.zeros(len(df), dtype="int8")
    cur = 0
    for i in range(len(df)):
        if bbv[i] != 0 and bbv[i] == ema5[i]:   # event: revert agrees with the EMA-5 side
            cur = int(bbv[i])
        elif cur != 0 and ema5[i] != cur:        # EMA-5 no longer supports -> exit to flat
            cur = 0
        out[i] = cur
    return pd.Series(out, index=df.index, dtype="int8").rename("vote_bb_reversal")


def vote_breakout_pullback(df: pd.DataFrame) -> pd.Series:
    """The trader's REAL 3-min entry: a Bollinger breakout, then a VRL retest that
    closes back below the 5-EMA.

    Confirmed from his 19-Jun charts: the FIRST upper-band breach (the bar's **HIGH**
    crosses the band, ``high > bb_upper`` — the close may still be inside) while above
    the 45-EMA is the trigger, and the **VRL = that breach bar's HIGH** (set once, fixed
    — NOT raised by later breaches). Price extends up to a peak, then
    **retraces back DOWN to the VRL**; the entry fires on the bar whose **low touches
    the VRL** (``low <= vrl``), the VRL **holds** (``close > vrl`` — not a breakdown),
    and the bar **closes below the 5-EMA** (``close < ema_5``). The 5-EMA close is the
    discriminator: an earlier retest that closes ABOVE the 5-EMA is NOT an entry (his
    14:03 vs 14:18). Mirror for short (first lower-band breach, VRL = its low; fire when
    ``high >= vrl and close < vrl and close > ema_5``). A close back through the 45-EMA
    cancels the armed setup. Latches FLAT after firing -> one entry per breach setup.

    Deliberately HIGH-RECALL on the fuzzy edges (touch vs close, the ``close>vrl``
    guard) — the genuine/false boundary is meant to be learned by the Claude trigger
    agent, not hardcoded. Needs ``close``, ``low``, ``high``, ``bb_upper``, ``bb_lower``,
    ``ema_5``, ``ema_45`` (all from ``engine.compute_indicators``).
    """
    close = df["close"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    bb_u = df["bb_upper"].to_numpy(dtype=float)
    bb_l = df["bb_lower"].to_numpy(dtype=float)
    ema5 = df["ema_5"].to_numpy(dtype=float)
    ema45 = df["ema_45"].to_numpy(dtype=float)

    out = np.zeros(len(df), dtype="int8")
    FLAT, LONG_ARM, SHORT_ARM = 0, 1, -1
    state = FLAT
    vrl = np.nan
    for i in range(len(df)):
        # NaN warm-up (bands/EMAs not ready) -> stay flat
        if np.isnan(bb_u[i]) or np.isnan(ema45[i]) or np.isnan(ema5[i]):
            state, vrl = FLAT, np.nan
            continue
        if state == FLAT:
            if high[i] > bb_u[i] and close[i] > ema45[i]:
                state, vrl = LONG_ARM, high[i]      # first up-breach (high crosses): VRL = its high
            elif low[i] < bb_l[i] and close[i] < ema45[i]:
                state, vrl = SHORT_ARM, low[i]      # first down-breach (low crosses): VRL = its low
        elif state == LONG_ARM:
            if close[i] < ema45[i]:                 # trend broke -> cancel
                state, vrl = FLAT, np.nan
            elif low[i] <= vrl and close[i] > vrl and close[i] < ema5[i]:
                out[i] = 1                          # retest the VRL + close below the 5-EMA
                state, vrl = FLAT, np.nan
        elif state == SHORT_ARM:
            if close[i] > ema45[i]:
                state, vrl = FLAT, np.nan
            elif high[i] >= vrl and close[i] < vrl and close[i] > ema5[i]:
                out[i] = -1
                state, vrl = FLAT, np.nan
    return pd.Series(out, index=df.index, dtype="int8").rename("vote_breakout_pullback")





def confirm_2_close(
    vote: pd.Series,
    df: pd.DataFrame,
    n_closes: int = 2,
    vol_window: int = 20,
) -> pd.Series:
    """Gate a vote with the journal's confirmation rule.

    "What confirms a signal? 2 closes + volume expanding + the stack agreeing —
    NOT a candle count." Keep a vote only where the same non-zero sign has held
    ``n_closes`` consecutive bars AND volume is expanding (``volume`` above its
    ``vol_window`` rolling mean); everything else -> 0 (flat).

    Zero/flat-volume instruments (FX / some indices, where volume is filled 0)
    have no volume signal, so the gate falls back to price-persistence only —
    it never blanks the whole series for want of volume. ``n_closes``/
    ``vol_window`` PROVISIONAL (see JOURNAL_EXTRACTION.md).
    """
    v = vote.astype(int)
    # same non-zero sign for the last n_closes bars
    persisted = pd.Series(True, index=v.index)
    for k in range(n_closes):
        persisted &= (v.shift(k) == v) & (v != 0)

    vol = df["volume"].astype(float)
    if vol.abs().sum() == 0:  # no usable volume -> price-persistence only
        vol_ok = pd.Series(True, index=v.index)
    else:
        # min_periods=2 so early bars aren't blanked waiting for a full window.
        vol_ok = vol > vol.rolling(vol_window, min_periods=2).mean()

    gated = v.where(persisted & vol_ok, 0)
    return gated.astype("int8").rename(f"{vote.name or 'vote'}_confirmed")


# Registry: name -> callable(df) -> vote Series. The config references voters by
# name so the indicator set is fully data-driven.
VOTERS = {
    "ema": vote_ema,
    "ema_stack": vote_ema_stack,
    "regime_45": vote_regime_45,
    "ema5_trigger": vote_ema5_trigger,
    "supertrend": vote_supertrend,
    "cpr": vote_cpr,
    "macd": vote_macd,
    "rsi": vote_rsi,
    "bollinger": vote_bollinger,
    "three_min": vote_three_min,
    "bb_reversal": vote_bb_reversal,
    "breakout_pullback": vote_breakout_pullback,
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
        default_factory=lambda: [
            "ema_stack", "supertrend", "macd", "rsi", "bollinger", "cpr"
        ]
    )
    voter_kwargs: dict[str, dict] = field(default_factory=dict)

    # confluence
    min_agree: int = 3

    # hierarchical
    primary: str = "ema_stack"
    confirm_min: int = 0
    veto: bool = True

    # confirmation gate (both methods): keep a call only after `confirm_closes`
    # persistent closes + expanding volume (the journal's 2-close rule). 0 = off.
    confirm_closes: int = 0
    confirm_vol_window: int = 20

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

    if cfg.confirm_closes > 0:  # journal's 2-close + volume confirmation gate
        gated = confirm_2_close(
            calls_to_sign(calls), df,
            n_closes=cfg.confirm_closes, vol_window=cfg.confirm_vol_window)
        calls = _sign_to_calls(gated)
    calls = calls.rename("direction")

    return (calls, votes) if return_votes else calls


# --------------------------------------------------------------------------- #
# Multi-timeframe (MTF) resolution
# --------------------------------------------------------------------------- #
# The 3-min strategy is read inside an MTF stack: 3m (trigger) + 15m/60m/daily/
# weekly (bias/regime). Higher TFs are resolved on their own bars, then aligned
# onto the 3m index WITHOUT lookahead (see indicators.timeframes.align_to_base).
# The combination of TFs is itself a config switch — same "let the backtest
# decide" philosophy as the single-TF resolver. See DIRECTIONAL_SPEC.md.

_MTF_METHODS = ("trigger_only", "htf_bias_trigger", "cross_tf_confluence", "per_tf_then_vote")


def calls_to_sign(calls: pd.Series) -> pd.Series:
    """Map a long/short/flat call Series to {+1, -1, 0}."""
    return calls.map(_CALL_TO_SIGN).astype("float").fillna(0).astype("int8")


def _sign_to_calls(sign: pd.Series) -> pd.Series:
    out = pd.Series(FLAT, index=sign.index, dtype=object)
    out[sign > 0] = LONG
    out[sign < 0] = SHORT
    return out.rename("direction")


@dataclass
class MTFDirectionalConfig:
    """MTF resolver config — extends the single-TF knobs with timeframe roles.

    Attributes:
        base: per-TF resolver config (voters, method, min_agree, ...) reused to
            resolve each timeframe individually.
        trigger_tf: the timeframe that fires the entry (the 3-min bar).
        bias_tfs: higher timeframes that set/veto direction.
        rules_by_tf: pandas resample rule per non-trigger TF, used by
            ``align_to_base`` to compute each HTF bar's close time.
        mtf_method: how the timeframes combine —
            ``htf_bias_trigger`` (default), ``cross_tf_confluence``,
            ``per_tf_then_vote``.
        bias_quorum: how much net agreement the bias TFs need to set a bias
            (and, in ``per_tf_then_vote``, the cross-TF agreement margin).
        veto: in ``htf_bias_trigger``, any bias TF opposing the net bias forces
            that bias to flat (so a conflicted regime stands the trade down).
    """

    base: DirectionalConfig = field(default_factory=DirectionalConfig)
    trigger_tf: str = "3min"
    bias_tfs: list[str] = field(
        default_factory=lambda: ["15min", "60min", "1day", "1week"]
    )
    rules_by_tf: dict[str, str] = field(
        default_factory=lambda: {
            "15min": "15min",
            "60min": "60min",
            "1day": "1D",
            "1week": "1W",
        }
    )
    mtf_method: str = "htf_bias_trigger"
    bias_quorum: int = 2
    veto: bool = True

    def validate(self) -> None:
        self.base.validate()
        if self.mtf_method not in _MTF_METHODS:
            raise ValueError(
                f"unknown mtf_method: {self.mtf_method!r}. Known: {_MTF_METHODS}"
            )
        for tf in self.bias_tfs:
            if tf not in self.rules_by_tf:
                raise ValueError(f"no resample rule configured for bias TF {tf!r}")


def _bias_sign_matrix(
    feats_by_tf: dict[str, pd.DataFrame],
    base_index: pd.DatetimeIndex,
    cfg: MTFDirectionalConfig,
) -> pd.DataFrame:
    """Per bias TF: resolve its own call, sign it, align (no-lookahead) to base."""
    cols = {}
    for tf in cfg.bias_tfs:
        call = resolve_direction(feats_by_tf[tf], cfg.base)
        sign = calls_to_sign(call)
        cols[tf] = align_to_base(sign, base_index, cfg.rules_by_tf[tf]).fillna(0)
    return pd.DataFrame(cols, index=base_index).astype(int)


def _resolve_htf_bias_trigger(
    feats_by_tf: dict[str, pd.DataFrame], cfg: MTFDirectionalConfig
) -> pd.Series:
    base_index = feats_by_tf[cfg.trigger_tf].index
    trig = calls_to_sign(resolve_direction(feats_by_tf[cfg.trigger_tf], cfg.base))

    B = _bias_sign_matrix(feats_by_tf, base_index, cfg)
    longs, shorts = (B > 0).sum(axis=1), (B < 0).sum(axis=1)
    net = longs - shorts
    bias = pd.Series(0, index=base_index, dtype="int8")
    bias[net >= cfg.bias_quorum] = 1
    bias[net <= -cfg.bias_quorum] = -1
    if cfg.veto:  # a conflicting higher TF cancels the bias
        bias[(bias == 1) & (shorts > 0)] = 0
        bias[(bias == -1) & (longs > 0)] = 0

    final = pd.Series(0, index=base_index, dtype="int8")
    take = (trig == bias) & (bias != 0)
    final[take] = trig[take]
    return _sign_to_calls(final)


def _aligned_votes(
    feats_by_tf: dict[str, pd.DataFrame], cfg: MTFDirectionalConfig
) -> pd.DataFrame:
    base_index = feats_by_tf[cfg.trigger_tf].index
    mats = []
    for tf, feats in feats_by_tf.items():
        votes = _collect_votes(feats, cfg.base)
        votes.columns = [f"{c}@{tf}" for c in votes.columns]
        if tf == cfg.trigger_tf:
            mats.append(votes.reindex(base_index))
        else:
            mats.append(align_to_base(votes, base_index, cfg.rules_by_tf[tf]))
    return pd.concat(mats, axis=1).reindex(base_index).fillna(0).astype(int)


def _resolve_cross_tf_confluence(
    feats_by_tf: dict[str, pd.DataFrame], cfg: MTFDirectionalConfig
) -> pd.Series:
    votes = _aligned_votes(feats_by_tf, cfg)
    return _resolve_confluence(votes, cfg.base)  # uses cfg.base.min_agree


def _resolve_per_tf_then_vote(
    feats_by_tf: dict[str, pd.DataFrame], cfg: MTFDirectionalConfig
) -> pd.Series:
    base_index = feats_by_tf[cfg.trigger_tf].index
    cols = {}
    for tf, feats in feats_by_tf.items():
        sign = calls_to_sign(resolve_direction(feats, cfg.base))
        if tf == cfg.trigger_tf:
            cols[tf] = sign.reindex(base_index).fillna(0)
        else:
            cols[tf] = align_to_base(sign, base_index, cfg.rules_by_tf[tf]).fillna(0)
    C = pd.DataFrame(cols, index=base_index).astype(int)
    net = (C > 0).sum(axis=1) - (C < 0).sum(axis=1)
    out = pd.Series(FLAT, index=base_index, dtype=object)
    out[net >= cfg.bias_quorum] = LONG
    out[net <= -cfg.bias_quorum] = SHORT
    return out.rename("direction")


def resolve_direction_mtf(
    feats_by_tf: dict[str, pd.DataFrame], cfg: MTFDirectionalConfig | None = None
) -> pd.Series:
    """Resolve a single long/short/flat call per 3-min bar from the MTF stack.

    Args:
        feats_by_tf: ``{tf_name: feature_frame}`` where each frame has already
            been through ``engine.compute_indicators`` (see
            ``timeframes.build_mtf_features``). Must contain ``cfg.trigger_tf``
            and every ``cfg.bias_tfs``.
        cfg: MTF config (defaults to htf_bias_trigger over 3m+15m/60m/1d/1w).

    Returns:
        ``Series[str]`` of calls aligned to the trigger (3-min) index.
    """
    cfg = cfg or MTFDirectionalConfig()
    cfg.validate()
    if cfg.trigger_tf not in feats_by_tf:
        raise ValueError(f"trigger_tf {cfg.trigger_tf!r} missing from feats_by_tf")

    if cfg.mtf_method == "trigger_only":
        return _resolve_trigger_only(feats_by_tf, cfg)
    if cfg.mtf_method == "htf_bias_trigger":
        return _resolve_htf_bias_trigger(feats_by_tf, cfg)
    if cfg.mtf_method == "cross_tf_confluence":
        return _resolve_cross_tf_confluence(feats_by_tf, cfg)
    return _resolve_per_tf_then_vote(feats_by_tf, cfg)


def _resolve_trigger_only(
    feats_by_tf: dict[str, pd.DataFrame], cfg: MTFDirectionalConfig
) -> pd.Series:
    """The 3-min trigger ALONE — no higher-TF gate (HTF is trend context only)."""
    return resolve_direction(feats_by_tf[cfg.trigger_tf], cfg.base)


# --------------------------------------------------------------------------- #
# Preset: the trader's journal 3-min strategy
# --------------------------------------------------------------------------- #
def journal_trigger_config() -> DirectionalConfig:
    """The trader's 3-min signal: a Bollinger **breakout + pullback CONTINUATION**
    (``breakout_pullback``) — an upper-band breakout above the 45-EMA arms a long,
    entered on the pullback to the 5-EMA (mirror for short). The breakout+pullback
    structure IS the confirmation (``confirm_closes=0``). The signal is the 3-min read
    alone — higher timeframes are trend context, not a gate (per the trader)."""
    return DirectionalConfig(
        method="confluence", voters=["breakout_pullback"], min_agree=1,
        confirm_closes=0)


def squeeze_trigger_config() -> DirectionalConfig:
    """SEPARATE squeeze-fade strategy (``bb_reversal``): a Bollinger breach -> revert
    after a volatility squeeze, EMA-5 confirmed, 2 closes + expanding volume. Not the
    trader's main entry — kept to study/compare (use via the harness ``--strategy``)."""
    return DirectionalConfig(
        method="confluence", voters=["bb_reversal"], min_agree=1,
        confirm_closes=2, confirm_vol_window=20)


def journal_mtf_config() -> MTFDirectionalConfig:
    """Journal strategy wired into the MTF stack as TRIGGER-ONLY (no HTF gate). The
    bias TFs are retained for trend display; they do not suppress the signal."""
    return MTFDirectionalConfig(
        base=journal_trigger_config(), trigger_tf="3min",
        bias_tfs=["15min", "60min", "1day", "1week"],
        mtf_method="trigger_only", bias_quorum=2, veto=True)


# --------------------------------------------------------------------------- #
# MTF 45-EMA confidence — multi-timeframe agreement grades conviction (not a gate)
# --------------------------------------------------------------------------- #
# The trade fires on the 3-min trio; confidence rises with each higher TF whose
# 45-EMA sits on the signal's side (price ABOVE it for a long, BELOW for a short).
CONF_TFS = ("15min", "30min", "60min", "1day", "1week")
CONF_RULES = {"15min": "15min", "30min": "30min", "60min": "60min",
              "1day": "1D", "1week": "1W"}


def mtf_ema45_alignment(
    feats_by_tf: dict[str, pd.DataFrame],
    base_index: pd.DatetimeIndex,
    tfs: tuple[str, ...] = CONF_TFS,
    rules: dict[str, str] = CONF_RULES,
) -> pd.DataFrame:
    """Per-bar sign of (3-min price − each TF's 45-EMA), no-lookahead aligned to base.

    ``+1`` price ABOVE that TF's 45-EMA (long-supportive), ``-1`` below, ``0`` for a
    missing TF or before its first bar closes. Each higher-TF 45-EMA is only visible
    once its bar has closed (``align_to_base``), so there is no future leakage.
    """
    base = feats_by_tf.get("3min")
    if base is None or "close" not in base.columns:
        return pd.DataFrame(index=base_index)   # nothing to compare price against
    price = base["close"].reindex(base_index)
    cols = {}
    for tf in tfs:
        if tf not in feats_by_tf or "ema_45" not in feats_by_tf[tf].columns:
            continue
        ema = align_to_base(feats_by_tf[tf]["ema_45"], base_index, rules[tf])
        diff = price - ema
        cols[tf] = (
            pd.Series(np.sign(diff.to_numpy()), index=base_index)
            .fillna(0).astype("int8")
        )
    return pd.DataFrame(cols, index=base_index)


def mtf_ema45_confidence(
    feats_by_tf: dict[str, pd.DataFrame],
    calls: pd.Series,
    tfs: tuple[str, ...] = CONF_TFS,
    rules: dict[str, str] = CONF_RULES,
) -> tuple[pd.Series, pd.DataFrame]:
    """Conviction score per bar: count of higher TFs whose 45-EMA agrees with the
    SIGNAL direction. Returns ``(conf, breakdown)`` — ``conf`` is ``0..len(tfs)`` (0
    when the call is flat), ``breakdown`` the per-TF ``+1/-1/0`` alignment frame.
    """
    align = mtf_ema45_alignment(feats_by_tf, calls.index, tfs, rules)
    sig = calls_to_sign(calls).reindex(align.index)
    if align.empty or align.shape[1] == 0:
        conf = pd.Series(0, index=align.index, dtype=int)
    else:
        agree = align.apply(lambda c: ((c == sig) & (sig != 0)).astype(int))
        conf = agree.sum(axis=1).astype(int)
    return conf.rename("mtf_confidence"), align
