"""3-min trigger-validation harness — paste a chart export, get the trigger times.

The calibration loop the trader runs to prove the 3-min strategy is correct:

  paste a TradingView-style 3-min export  ->  this tool prints every trigger
  timestamp + WHY it fired  ->  the trader checks each on the real chart  ->
  reports genuine / false / missed  ->  we correct the trigger code and re-run.

The strategy is the journal's event-gated Bollinger reversal (``vote_bb_reversal``):
a squeeze-gated Bollinger breach -> revert whose close is on the matching side of
the EMA-5 ARMS a direction, held while the EMA-5 holds that side, confirmed by 2
closes (+ expanding volume when present). It is instrument/timeframe-agnostic — it
reads only close vs EMA-5 + Bollinger; OI is a separate analysis and not used here.

Two modes:
  * ``platform`` (default) — run the trigger on the export's OWN indicator values
    (Bollinger Top/Median/Bottom + EMA-5). This isolates the *trigger logic* from any
    indicator-calc difference, which is what we want to validate. ``bb_width`` is
    derived from the platform bands exactly as the engine defines it.
  * ``--recompute`` — recompute every indicator from OHLC with ``compute_indicators``
    (validates the whole pipeline instead of just the logic).

Reuses ``scoring.validate_export.load_export`` (already parses this export format),
``indicators.directional.journal_trigger_config`` / ``resolve_direction``, and mirrors
``indicators.engine.bollinger_vrl_breakout`` so the squeeze/VRL read matches the engine.

CLI:
  python -m scoring.trigger_check data/validate/<name>.txt
  python -m scoring.trigger_check <file> --events
  python -m scoring.trigger_check <file> --at 13:45
  python -m scoring.trigger_check <file> --recompute --squeeze-window 50 --squeeze-pct 0.25
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

from scoring.validate_export import load_export
from indicators.engine import compute_indicators
from indicators.directional import (
    resolve_direction, journal_trigger_config, squeeze_trigger_config,
)

# columns the breakout-pullback / squeeze triggers + diagnostic read
_NEEDED = ("bb_upper", "bb_mid", "bb_lower", "ema_5", "ema_45")


# --------------------------------------------------------------------------- #
# Build the feature frame the trigger reads
# --------------------------------------------------------------------------- #
def _bb_vrl_from_bands(
    feats: pd.DataFrame, squeeze_window: int, squeeze_pct: float
) -> pd.DataFrame:
    """Mirror ``engine.bollinger_vrl_breakout`` using whatever bb_* are in ``feats``.

    Adds the diagnostic columns and ``sig_bb_vrl``. ``bb_width`` must already exist.
    """
    close = feats["close"]
    width = feats["bb_width"]
    sq_thresh = width.rolling(squeeze_window).quantile(squeeze_pct)
    was_squeezed = width.shift(1) <= sq_thresh.shift(1)
    expanding = width > width.shift(1)
    gate = was_squeezed & expanding

    prev_below = close.shift(1) < feats["bb_lower"].shift(1)
    prev_above = close.shift(1) > feats["bb_upper"].shift(1)
    recover_up = prev_below & (close > feats["bb_lower"])
    recover_dn = prev_above & (close < feats["bb_upper"])

    sig = pd.Series(0, index=feats.index, dtype="int8")
    sig[recover_up & gate] = 1
    sig[recover_dn & gate] = -1

    feats["squeeze_thresh"] = sq_thresh
    feats["was_squeezed"] = was_squeezed
    feats["expanding"] = expanding
    feats["prev_outside"] = np.where(prev_below, "below", np.where(prev_above, "above", "-"))
    feats["sig_bb_vrl"] = sig
    return feats


def build_feats(
    path: str, mode: str, tz: str, squeeze_window: int, squeeze_pct: float
) -> pd.DataFrame:
    """Assemble the feature frame the trigger reads, in ``platform`` or ``recompute`` mode."""
    ohlcv, platform = load_export(path, tz)

    if mode == "recompute":
        feats = compute_indicators(ohlcv)
        # recompute mode keeps engine bb_vrl; add diagnostic squeeze cols for display
        feats = _bb_vrl_from_bands(feats, squeeze_window, squeeze_pct)
        return feats

    # platform mode: use the trader's OWN indicator values
    missing = [c for c in _NEEDED if c not in platform.columns]
    if missing:
        raise SystemExit(
            f"export is missing {missing} (needed for platform mode); columns present: "
            f"{list(platform.columns)}. Use --recompute to compute from OHLC instead."
        )
    feats = ohlcv.copy()
    for c in _NEEDED:
        feats[c] = platform[c]
    feats["bb_width"] = (feats["bb_upper"] - feats["bb_lower"]) / feats["bb_mid"]
    feats["sig_ema5_trigger"] = np.sign(feats["close"] - feats["ema_5"]).astype("int8")
    feats = _bb_vrl_from_bands(feats, squeeze_window, squeeze_pct)
    return feats


# --------------------------------------------------------------------------- #
# Trigger detection (same flip rule as analysis.triggers.list_triggers)
# --------------------------------------------------------------------------- #
def find_triggers(calls: pd.Series) -> list[dict]:
    out = []
    c = calls.to_numpy()
    for i in range(len(c)):
        prev = c[i - 1] if i > 0 else "flat"
        if c[i] in ("long", "short") and c[i] != prev:
            out.append({"i": i, "ts": calls.index[i], "direction": c[i]})
    return out


def _hhmm(ts) -> str:
    return pd.Timestamp(ts).strftime("%H:%M")


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _warmup_note(feats: pd.DataFrame, strategy: str, squeeze_window: int) -> str:
    n = len(feats)
    if strategy == "squeeze":
        first_ok = feats["squeeze_thresh"].first_valid_index()
        when = _hhmm(first_ok) if first_ok is not None else "never (need more bars)"
        return (f"Warm-up: the squeeze needs {squeeze_window} prior bars; only bars from "
                f"~{when} are testable here ({n} bars total). Paste from the 09:15 open "
                f"(or include >= {squeeze_window} bars before the time you're checking).")
    # breakout: only needs Bollinger + the EMAs (supplied in platform mode -> no warm-up)
    ok = feats[["bb_upper", "ema_45", "ema_5"]].dropna()
    when = _hhmm(ok.index[0]) if len(ok) else "never (need more bars)"
    return (f"Breakout strategy: bars testable from ~{when} ({n} bars total; "
            f"in platform mode the bands/EMAs are supplied, so no warm-up).")


def report_triggers(feats: pd.DataFrame, calls: pd.Series, strategy: str,
                    squeeze_window: int) -> None:
    trigs = find_triggers(calls)
    hint = "--candidates" if strategy == "breakout" else "--events"
    print(f"\n{len(trigs)} trigger(s):")
    for t in trigs:
        print(f"  {_hhmm(t['ts'])}  {t['direction'].upper()}  "
              f"(close {feats['close'].iloc[t['i']]:.2f})")
    if not trigs:
        print(f"  (none — see {hint} to see which setups were armed/filtered)")
    print("\n" + _warmup_note(feats, strategy, squeeze_window))


def report_candidates(feats: pd.DataFrame, calls: pd.Series) -> None:
    """Replay the breakout -> first-5-EMA-pullback state machine and show each setup."""
    close = feats["close"].to_numpy(float)
    low = feats["low"].to_numpy(float); high = feats["high"].to_numpy(float)
    bb_u = feats["bb_upper"].to_numpy(float); bb_l = feats["bb_lower"].to_numpy(float)
    e5 = feats["ema_5"].to_numpy(float); e45 = feats["ema_45"].to_numpy(float)
    idx = feats.index
    print("\nBreakout -> first 5-EMA pullback (one entry per setup, re-arm on a new breakout):")
    state, arm_ts, n = 0, None, 0
    for i in range(len(feats)):
        if np.isnan(bb_u[i]) or np.isnan(e45[i]) or np.isnan(e5[i]):
            continue
        if state == 0:
            if high[i] > bb_u[i] and close[i] > e45[i] and close[i] >= e5[i]:
                state, arm_ts = 1, idx[i]
                print(f"  {_hhmm(arm_ts)} up-breach (high {high[i]:.2f}>{bb_u[i]:.2f}) -> ARM long")
            elif low[i] < bb_l[i] and close[i] < e45[i] and close[i] <= e5[i]:
                state, arm_ts = -1, idx[i]
                print(f"  {_hhmm(arm_ts)} down-breach (low {low[i]:.2f}<{bb_l[i]:.2f}) -> ARM short")
        elif state == 1:
            if close[i] < e45[i]:
                print(f"    {_hhmm(idx[i])} CANCELLED (closed below 45-EMA)")
                state = 0
            elif close[i] < e5[i]:
                print(f"    {_hhmm(idx[i])} FIRED LONG: first close {close[i]:.2f} below 5-EMA {e5[i]:.2f}")
                state, n = 0, n + 1
        elif state == -1:
            if close[i] > e45[i]:
                print(f"    {_hhmm(idx[i])} CANCELLED (closed above 45-EMA)")
                state = 0
            elif close[i] > e5[i]:
                print(f"    {_hhmm(idx[i])} FIRED SHORT: first close {close[i]:.2f} above 5-EMA {e5[i]:.2f}")
                state, n = 0, n + 1
    if state != 0:
        print(f"    (setup armed at {_hhmm(arm_ts)} still waiting for a pullback at end of data)")
    if n == 0:
        print("  (no fired setups)")


def report_events(feats: pd.DataFrame, calls: pd.Series) -> None:
    """Every Bollinger reversal event and its fate (confirmed trigger / filtered why)."""
    ev = feats.index[feats["sig_bb_vrl"] != 0]
    trig_ts = {t["ts"] for t in find_triggers(calls)}
    print(f"\n{len(ev)} Bollinger reversal event(s):")
    if len(ev) == 0:
        print("  (no squeeze-gated breach->revert in this window)")
    for ts in ev:
        row = feats.loc[ts]
        bbv = int(row["sig_bb_vrl"])
        ema5 = int(row.get("sig_ema5_trigger", np.sign(row["close"] - row["ema_5"])))
        side = "long" if bbv > 0 else "short"
        agree = bbv == ema5
        # did this bar (or its run) become a confirmed trigger nearby?
        confirmed = any(abs((pd.Timestamp(t) - pd.Timestamp(ts)).total_seconds()) <= 600
                        for t in trig_ts)
        if not agree:
            fate = f"FILTERED — EMA-5 side disagrees (bb says {side}, close is "
            fate += "above" if ema5 > 0 else ("below" if ema5 < 0 else "on") + " EMA-5)"
        elif confirmed:
            fate = "CONFIRMED -> trigger (held 2 closes)"
        else:
            fate = "FILTERED — armed but failed the 2-close confirm (didn't hold)"
        print(f"  {_hhmm(ts)}  bb_vrl={bbv:+d} ema5={ema5:+d}  {fate}")


def report_at(feats: pd.DataFrame, calls: pd.Series, at: str, window: int = 4) -> None:
    """Dump the per-bar breakdown around a chosen HH:MM (or full timestamp)."""
    times = pd.Series(feats.index).dt.strftime("%H:%M")
    matches = feats.index[(times == at).to_numpy()]
    if len(matches) == 0:
        print(f"\nNo bar at {at}. Available: {times.iloc[0]}..{times.iloc[-1]}")
        return
    pos = feats.index.get_loc(matches[0])
    lo, hi = max(0, pos - window), min(len(feats), pos + window + 1)
    cols = ["close", "ema_5", "bb_lower", "bb_upper", "bb_width", "squeeze_thresh",
            "was_squeezed", "expanding", "prev_outside", "sig_bb_vrl"]
    if "sig_ema5_trigger" in feats.columns:
        cols.append("sig_ema5_trigger")
    view = feats.iloc[lo:hi][cols].copy()
    view.insert(0, "call", calls.iloc[lo:hi].to_numpy())
    view.index = [_hhmm(t) for t in view.index]
    print(f"\nBreakdown around {at}:")
    with pd.option_context("display.max_columns", None, "display.width", 200,
                           "display.float_format", lambda x: f"{x:.2f}"):
        print(view.to_string())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("export", help="path to the chart export (.txt/.csv)")
    ap.add_argument("--mode", choices=["platform", "recompute"], default="platform",
                    help="platform: use the export's own indicators (default); "
                         "recompute: compute indicators from OHLC")
    ap.add_argument("--recompute", action="store_true", help="alias for --mode recompute")
    ap.add_argument("--strategy", choices=["breakout", "squeeze"], default="breakout",
                    help="breakout: breakout+pullback continuation (default, the real "
                         "3-min entry); squeeze: the separate Bollinger squeeze fade")
    ap.add_argument("--tz", default="Asia/Kolkata")
    ap.add_argument("--squeeze-window", type=int, default=50)
    ap.add_argument("--squeeze-pct", type=float, default=0.25)
    ap.add_argument("--confirm-closes", type=int, default=None,
                    help="override the strategy's confirm gate (default: per strategy)")
    ap.add_argument("--candidates", action="store_true",
                    help="list every breakout->pullback setup + its fate (breakout)")
    ap.add_argument("--events", action="store_true", help="list every squeeze reversal event + fate")
    ap.add_argument("--at", help="dump the per-bar breakdown around HH:MM")
    args = ap.parse_args(argv)

    mode = "recompute" if args.recompute else args.mode
    feats = build_feats(args.export, mode, args.tz, args.squeeze_window, args.squeeze_pct)

    cfg = squeeze_trigger_config() if args.strategy == "squeeze" else journal_trigger_config()
    if args.confirm_closes is not None:
        cfg.confirm_closes = args.confirm_closes
    calls = resolve_direction(feats, cfg)

    print(f"Loaded {len(feats)} bars from {args.export}  [strategy={args.strategy}, "
          f"mode={mode}, confirm={cfg.confirm_closes}]")
    report_triggers(feats, calls, args.strategy, args.squeeze_window)
    if args.candidates:
        report_candidates(feats, calls)
    if args.events:
        report_events(feats, calls)
    if args.at:
        report_at(feats, calls, args.at)
    return 0


if __name__ == "__main__":
    sys.exit(main())
