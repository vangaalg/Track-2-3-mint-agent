"""Backtest the journal 3-min breakout-pullback trigger over a history window.

The trigger engine already exists: ``analysis.triggers.list_triggers`` enumerates
EVERY Trade-1 trigger across a multi-session frame and resolves each to win/loss/open
within its own session (session-low stop + structural/R-multiple target). This module
wraps it into a one-call backtest:

    pull ~N days of 1-minute NIFTY (+ long daily) -> build the MTF ladder
    -> list_triggers (the locked breakout-pullback resolver) -> aggregate.

Aggregation is pure + testable (``aggregate``); the CLI does the (networked) pull via
``loaders.get_loader`` and writes a ranked CSV + a markdown summary to results/.

Live data pulls run on the user's machine (creds in env); this stays import-clean and
the engine is exercised offline with synthetic/already-built frames.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, timedelta

import pandas as pd

from analysis.triggers import list_triggers
from analysis.trade1 import LOT_SIZE
from indicators.directional import journal_mtf_config
from feeds.snapshot import build_snapshot


def _stats(rows: list[dict], lot_size: int, lots: int) -> dict:
    wins = [r for r in rows if r["outcome"] == "win"]
    losses = [r for r in rows if r["outcome"] == "loss"]
    eods = [r for r in rows if r["outcome"] not in ("win", "loss")]   # exited at the close
    decided = len(wins) + len(losses)
    net_pts = round(sum(r["points"] for r in rows), 2)                # all exits realised
    gross_win = sum(r["points"] for r in wins)
    gross_loss = sum(r["points"] for r in losses)                     # negative
    gains = sum(r["points"] for r in rows if r["points"] > 0)         # incl. eod, by sign
    pains = sum(r["points"] for r in rows if r["points"] < 0)
    n = len(rows)
    return {
        "n": n, "wins": len(wins), "losses": len(losses), "eod": len(eods),
        "hit_rate": round(len(wins) / decided, 3) if decided else None,   # target vs stop
        "net_points": net_pts,
        "net_rupees": round(net_pts * lot_size * lots, 0),
        "eod_points": round(sum(r["points"] for r in eods), 2),
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(gross_loss / len(losses), 2) if losses else None,
        "expectancy": round(net_pts / n, 2) if n else None,               # per trade, all exits
        "profit_factor": round(gains / abs(pains), 2) if pains else None,
    }


def aggregate(triggers: list[dict], lot_size: int = LOT_SIZE, lots: int = 1) -> dict:
    """Roll a list of trigger dicts (from ``list_triggers``) into a backtest report.

    Returns overall stats + per-direction + per-day breakdowns. Pure — pass any list
    of trigger dicts carrying ``outcome``/``points``/``direction``/``date``.
    """
    overall = _stats(triggers, lot_size, lots)
    by_dir = {d: _stats([r for r in triggers if r["direction"] == d], lot_size, lots)
              for d in ("long", "short")}
    days = defaultdict(list)
    for r in triggers:
        days[r["date"]].append(r)
    by_day = [{"date": d, **_stats(rows, lot_size, lots)} for d, rows in sorted(days.items())]
    return {"overall": overall, "by_direction": by_dir, "by_day": by_day,
            "lot_size": lot_size, "lots": lots}


def make_claude_filter(symbol: str, base_1m, daily, memory: str = "",
                       completer=None, cfg=None):
    """Build a per-trigger Claude take/skip filter for the backtest.

    Returns ``fn(trigger) -> "enter"|"stand_down"``: rebuilds the AS-OF world at the
    trigger's timestamp (``build_snapshot_at`` — no future leakage), runs the engine
    proposal + ``claude_read`` against it, and returns Claude's verdict. ``completer``
    is the injectable Anthropic seam (stub it in tests); live needs ANTHROPIC_API_KEY.
    Errors fall back to "stand_down" (skip) so one bad bar can't kill the run.
    """
    from feeds.snapshot import build_snapshot_at
    from analysis.trade1 import propose_trade1
    from agent.read import claude_read
    cfg = cfg or journal_mtf_config()

    def fn(trigger: dict) -> dict:
        try:
            snap = build_snapshot_at(symbol, base_1m, daily, trigger["ts"], mtf_cfg=cfg)
            prop = propose_trade1(snap)
            read = claude_read(snap, prop, memory, completer=completer)
            return {"verdict": "enter" if read.recommendation == "enter" else "stand_down",
                    "target": read.proposed_target, "stop": read.proposed_stop}
        except Exception:
            return {"verdict": "stand_down"}

    return fn


def clamp_levels(direction: str, entry: float, target, stop,
                 min_rr: float = 1.5, max_risk_frac: float = 0.02):
    """Guardrail Claude's proposed target/stop. Returns ``(target, stop, rr)`` or
    ``(None, None, None)`` if unusable (wrong side / zero risk).

    Both must sit on the correct side of entry; an absurdly wide stop is capped to
    ``max_risk_frac`` of price; and if Claude's reward:risk is below ``min_rr`` the
    target is pushed out to meet it (we never tighten Claude's stop past its idea).
    """
    if target is None or stop is None:
        return None, None, None
    long = direction == "long"
    if long and not (target > entry and stop < entry):
        return None, None, None
    if (not long) and not (target < entry and stop > entry):
        return None, None, None
    risk = abs(entry - stop)
    if risk <= 0:
        return None, None, None
    if risk > max_risk_frac * entry:                 # sanity-cap an insane stop
        risk = max_risk_frac * entry
        stop = entry - risk if long else entry + risk
    reward = abs(target - entry)
    if reward / risk < min_rr:                        # honour the R:R floor via the target
        reward = min_rr * risk
        target = entry + reward if long else entry - reward
    return round(target, 2), round(stop, 2), round(reward / risk, 2)


def run_backtest(snap, lots: int = 1, cfg=None, target_driven: bool = True,
                 claude_filter=None) -> dict:
    """Backtest the trigger over a pre-built snapshot's feats/frames.

    Reuses the LIVE resolver (``journal_mtf_config``) so the backtest fires exactly the
    triggers the cockpit + training would. ``target_driven`` uses the target-first
    level model (SL derived from R:R). If ``claude_filter`` is given, each trigger is
    tagged with Claude's take/skip verdict and a CLAUDE-FILTERED (ENTER-only) report
    is added alongside the unfiltered one.

    Returns ``{"triggers": [...], "report": {...}, "filtered": {...}|None}``.
    """
    from analysis.triggers import _resolve_intraday
    cfg = cfg or journal_mtf_config()
    triggers = list_triggers(snap.feats, snap.frames, cfg=cfg, size_lots=lots,
                             lot_size=LOT_SIZE, realistic=True, target_driven=target_driven)
    filtered = None
    if claude_filter is not None:
        frame3m = snap.frames["3min"]
        taken = []
        for t in triggers:
            cf = claude_filter(t)
            if isinstance(cf, str):                  # back-compat: verdict-only filters
                cf = {"verdict": cf}
            t["claude"] = cf["verdict"]
            tgt, stp, rr = clamp_levels(t["direction"], t["entry"], cf.get("target"), cf.get("stop"))
            if tgt is not None:
                t["claude_target"], t["claude_stop"], t["claude_rr"] = tgt, stp, rr
            if cf["verdict"] != "enter":
                continue
            if tgt is not None:                      # trade CLAUDE's own levels
                outcome, exit_px, points, _ = _resolve_intraday(
                    frame3m, t["ts"], t["direction"], t["entry"], stp, tgt)
                taken.append({**t, "eng_stop": stp, "eng_target": tgt, "eng_rr": rr,
                              "outcome": outcome, "points": points,
                              "rupees": round(points * LOT_SIZE * lots, 0)})
            else:                                    # no usable levels -> engine's
                taken.append(t)
        filtered = aggregate(taken, LOT_SIZE, lots)
    return {"triggers": triggers, "report": aggregate(triggers, LOT_SIZE, lots),
            "filtered": filtered}


# --------------------------------------------------------------------------- #
# CLI — pull + report
# --------------------------------------------------------------------------- #
def _pull(symbol: str, days: int, loader_name: str, chunk_days: int = 3):
    """Pull ~`days` of 1-minute base + a long daily history (runs on the user's box).

    Breeze's get_historical_data_v2 caps rows per call (~1000 ≈ ~3 trading days of
    1-minute bars), so a single 30-day request silently truncates to the most recent
    window. We PAGINATE: walk the range in `chunk_days`-wide windows, pull each, and
    concatenate (dedup + sort). Empty windows (weekends/holidays) are skipped.
    """
    import time
    from loaders import get_loader
    loader = get_loader(loader_name)
    today = date.today()
    cur = today - timedelta(days=days + 4)
    parts: list[pd.DataFrame] = []
    while cur <= today:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), today)
        try:
            part = loader.load(symbol, "minute", start=cur, end=chunk_end, use_cache=False)
            if part is not None and not part.empty:
                parts.append(part)
                print(f"  {cur} … {chunk_end}: {len(part)} bars", file=sys.stderr)
        except Exception as exc:                       # empty window or transient error
            print(f"  {cur} … {chunk_end}: skipped ({exc})", file=sys.stderr)
        cur = chunk_end + timedelta(days=1)
        time.sleep(0.3)                                # gentle on Breeze rate limits
    if not parts:
        raise RuntimeError(f"no 1-minute data pulled for {symbol!r} over {days}d — "
                           "check creds/session token and the symbol.")
    base = pd.concat(parts)
    base = base[~base.index.duplicated(keep="first")].sort_index()
    daily = loader.load(symbol, "day", start=today - timedelta(days=800), use_cache=False)
    return base, daily


def _fmt(s: dict) -> str:
    hit = "—" if s["hit_rate"] is None else f"{s['hit_rate'] * 100:.0f}%"
    return (f"n={s['n']}  W/L/EOD={s['wins']}/{s['losses']}/{s['eod']}  hit={hit}  "
            f"net={s['net_points']:+.1f} pts (₹{s['net_rupees']:+,.0f})  "
            f"exp={s['expectancy']} pf={s['profit_factor']}")


def report_text(symbol: str, report: dict, levels: str = "target",
                filtered: dict | None = None) -> str:
    o = report["overall"]
    stop_desc = "target-first SL from R:R" if levels == "target" else "session-low stop"
    lines = [f"Backtest — {symbol}  (breakout-pullback, {stop_desc}, R:R≥1.5, "
             f"one position at a time, flat by close; {report['lots']} lot × {report['lot_size']})",
             "hit = target-vs-stop only · EOD = exited at the bell (P&L still counted) · "
             "exp = net/trade · pf incl. EOD by sign", ""]
    lines.append(f"OVERALL  {_fmt(o)}")
    lines.append(f"  long   {_fmt(report['by_direction']['long'])}")
    lines.append(f"  short  {_fmt(report['by_direction']['short'])}")
    if filtered is not None:
        lines.append("")
        lines.append(f"CLAUDE-FILTERED (took {filtered['overall']['n']} of {o['n']}):"
                     f"  {_fmt(filtered['overall'])}")
        lines.append(f"  long   {_fmt(filtered['by_direction']['long'])}")
        lines.append(f"  short  {_fmt(filtered['by_direction']['short'])}")
    lines.append("")
    lines.append("Per day:")
    for d in report["by_day"]:
        lines.append(f"  {d['date']}  {_fmt(d)}")
    return "\n".join(lines)


def write_outputs(symbol: str, triggers: list[dict], report: dict, outdir: str,
                  levels: str = "target", filtered: dict | None = None) -> tuple[str, str]:
    from pathlib import Path
    Path(outdir).mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    csv_path = f"{outdir}/backtest_{symbol}_{stamp}.csv"
    md_path = f"{outdir}/backtest_{symbol}_{stamp}.md"
    pd.DataFrame(triggers).to_csv(csv_path, index=False)
    Path(md_path).write_text(
        "```\n" + report_text(symbol, report, levels=levels, filtered=filtered) + "\n```\n",
        encoding="utf-8")
    return csv_path, md_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--days", type=int, default=30, help="lookback window (~1 month default)")
    ap.add_argument("--loader", default="breeze", help="loaders.get_loader name (breeze/twelvedata)")
    ap.add_argument("--lots", type=int, default=1, help="position size for the ₹ column")
    ap.add_argument("--chunk-days", type=int, default=3,
                    help="pagination window for the 1-min pull (Breeze caps rows/call)")
    ap.add_argument("--levels", choices=["target", "stop"], default="target",
                    help="target = SL derived from R:R off the objective; stop = session-low stop")
    ap.add_argument("--claude", action="store_true",
                    help="run Claude take/skip on each trigger (needs ANTHROPIC_API_KEY; slow)")
    ap.add_argument("--out", default="results", help="output dir for the CSV + markdown")
    args = ap.parse_args(argv)

    print(f"Pulling ~{args.days}d of {args.symbol} 1-min via '{args.loader}' "
          f"(paginated, {args.chunk_days}d/chunk; needs creds + network)…", file=sys.stderr)
    base, daily = _pull(args.symbol, args.days, args.loader, args.chunk_days)
    snap = build_snapshot(args.symbol, base, daily, mtf_cfg=journal_mtf_config())
    cfilter = None
    if args.claude:
        print("Running Claude take/skip per trigger (this is slow)…", file=sys.stderr)
        cfilter = make_claude_filter(args.symbol, base, daily)
    out = run_backtest(snap, lots=args.lots, target_driven=(args.levels == "target"),
                       claude_filter=cfilter)
    print(report_text(args.symbol, out["report"], levels=args.levels, filtered=out["filtered"]))
    csv_path, md_path = write_outputs(args.symbol, out["triggers"], out["report"], args.out,
                                      levels=args.levels, filtered=out["filtered"])
    print(f"\nWrote {csv_path}\n      {md_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
